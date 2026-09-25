"""Session teardown and identity-retirement boundary.

This leaf module owns the lifecycle state that survives across individual
provider objects, while the ``SessionManager`` facade remains the authority for
session allocation, warm-pool state, background runtimes, compaction policy,
and persistence.  Cross-boundary calls deliberately route through ``owner`` so
existing instance monkeypatch seams remain observable after wiring.

There is no runtime import of :mod:`kiro_crew.session`.  Patchable module
globals, provider types, process helpers, and policy constants are resolved by
call-time dependencies supplied by the facade.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from kiro_crew.kiro_prerequisite import (
    identity_park_grace_remaining,
    identity_stamp_mismatch,
    mark_identity_parked,
)
from kiro_crew.messaging import turn_ceiling
from kiro_crew.messaging.link import canonical_key
from kiro_crew.metrics.sessions import (
    END_REASON_DESTROYED,
    END_REASON_DISCARDED,
    END_REASON_REMOVED,
    END_REASON_RESET,
    END_REASON_RETIRED,
    END_REASON_SHUTDOWN,
    END_REASON_UNCLAIMED,
    record_session_ended,
    record_sessions_ended,
)

CancelOutcome = Literal["acked", "timeout", "no_turn", "error"]

#: The teardown reason a destroy records when it could NOT establish that the ACP
#: id is globally revoked. Retention authorizes deleting a unit's history only on
#: ``destroyed``, which means "revocation completed", so this word records the same
#: teardown while leaving the log in place. The map delete in ``destroy`` removes
#: exactly one key, and two keys can legitimately point at one sid -- importing a
#: transferred session twice allocates a new slot key each time and leaves the
#: source intact -- so the tidier record is the wrong one to claim.
_END_REASON_SID_RETAINED = "destroyed_sid_retained"

#: Stands in for "a key may still map to this sid, but the map could not be read".
#: Not a real key, and never logged as one: it only has to be non-None so the claim
#: is withheld, because an unreadable map is not evidence of revocation.
_SID_RETENTION_UNKNOWN = "<unreadable session map>"
StopOutcome = Literal["soft", "hard", "idle"]
ProviderFactory = Callable[..., Any]
_ANY_SESSION = object()

#: Budget for cancelling one parent's sub-agent runs at a parent end. A parent
#: end is on a user-facing path (a closed tab, a switched model), so an
#: unresponsive child must not hold it open; the companion-runtime release that
#: follows is the backstop for whatever the cancel does not reach in time.
_CHILD_CANCEL_TIMEOUT_SECS = 20.0


class _RecycleCallback(Protocol):
    async def __call__(self, key: str, *, reason: str) -> None: ...


class _ChildTeardownHandler(Protocol):
    """Ends the sub-agent runs a parent spawned, in two halves.

    Satisfied by ``SubagentManager``. The halves are separate because they must
    run at different moments: the snapshot while this module still holds the
    registry lock, the cancel after the provider teardown awaits.
    """

    def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]: ...

    async def cancel_for_teardown(
        self,
        agent_ids: "Sequence[str]",
        *,
        parent_session_key: str,
        verb: str = "",
    ) -> int: ...


class _SessionEntry(Protocol):
    provider: Any
    semaphore: asyncio.BoundedSemaphore
    first_turn: object
    provider_switch_replay: bool
    retire_on_identity_change: bool
    prev_turn_cancelled: bool


class _SessionMapPort(Protocol):
    def clear_sid(self, key: str) -> None: ...

    def delete(self, key: str, *, reason: str | None = None) -> None: ...

    #: Read-only, and used ONLY to withhold a claim: a destroy asks whether any
    #: other key still maps the session id it just unmapped, and records a
    #: non-terminal teardown reason when one does.
    def find_key_by_sid(self, session_id: str, *, exclude: str = "") -> str | None: ...

    def set(
        self,
        key: str,
        sid: str,
        *,
        provider: str,
        cwd: str | None = None,
    ) -> None: ...

    async def aclose(self) -> None: ...


class _BackgroundRuntime(Protocol):
    def has_active_or_initializing_sessions(self) -> bool: ...

    def is_alive(self) -> bool: ...

    async def kill(self, expected: bool = False, reason: str = "") -> None: ...


class SessionLifecycleOwner(Protocol):
    """Facade state and operations consumed by the lifecycle service."""

    _cfg: Any
    _provider_factory: ProviderFactory | None
    _sessions: MutableMapping[str, _SessionEntry]
    _lock: asyncio.Lock
    _closing: bool
    _update_pause_owned: bool
    _start_sem: asyncio.Semaphore
    _starting_pids: set[int]

    _pool_fill_lock: asyncio.Lock
    _warm_pool: asyncio.Queue[tuple[Any, float]]
    _pool_size: int
    _pool_agent: str
    _pool_ttl_secs: int
    _pool_cwd: str
    _pool_started: bool
    _pool_health_task: asyncio.Task[Any] | None

    _compact_cooldown_until: MutableMapping[str, float]
    _compact_pending_verdict: MutableMapping[str, float]
    _cleanup_task: asyncio.Task[Any] | None
    _background_tasks: set[asyncio.Task[Any]]

    _bg_runtime_lock: asyncio.Lock
    _bg_runtime: _BackgroundRuntime | None
    _draining_bg_runtimes: list[_BackgroundRuntime]
    _subagent_runtimes: MutableMapping[str, _BackgroundRuntime]
    _subagent_runtime_locks: MutableMapping[str, asyncio.Lock]
    _draining_subagent_runtimes: list[_BackgroundRuntime]

    _session_map: _SessionMapPort

    def _fold_key(self, key: str) -> str: ...

    def _has_pending_injection(self, key: str) -> bool: ...

    def _has_allocation_reservation(self, key: str) -> bool: ...

    def session_generation(self, key: str) -> int: ...

    def _advance_session_generation(self, key: str) -> int: ...

    def set_autocompact_pct(self, key: str, pct: float | None) -> None: ...

    def _is_continuable_key(self, key: str) -> bool: ...

    def clear_queue(self, key: str, owned_by: Callable[[dict], bool] | None = None) -> None: ...

    def release(self, key: str) -> None: ...

    async def _discard_pool_provider(self, provider: Any, context: str) -> None: ...

    async def start_pool(self, *, blocking: bool = True) -> None: ...

    async def _retire_stale_backend_bg_runtime(self) -> None: ...

    async def release_subagent_runtime(self, parent_session_key: str) -> None: ...

    async def _retire_kiro_warm_pool(self) -> bool: ...

    def _mark_identity_epoch(self) -> None: ...

    async def _retire_kiro_subagent_runtimes(self) -> bool: ...

    async def _retire_kiro_bg_runtime(self) -> bool: ...

    async def _reap_drained_bg_runtimes_locked(self) -> None: ...

    async def _detach_bg_runtime_locked(
        self, runtime: _BackgroundRuntime, cause: str, *, park_only: bool = False
    ) -> None: ...

    async def drain_active_turns(self, timeout: float | None = None) -> int: ...

    async def reset(
        self,
        key: str,
        *,
        expect_session: _SessionEntry | None = None,
        skip_if_busy: bool = False,
        skip_if_injecting: bool = False,
        refuse_only_on_active_turn: bool = False,
        clear_conversation: bool = False,
        ends_conversation: bool = False,
    ) -> bool: ...

    async def _send_abort_for_session(self, key: str, session: Any) -> None: ...

    async def _eager_respawn(self, key: str) -> None: ...

    async def get_or_create(self, key: str, **kwargs: Any) -> tuple[Any, bool, bool]: ...


@dataclass(frozen=True, slots=True)
class SessionLifecycleConstants:
    """Patch-sensitive policy values resolved as one call-time snapshot."""

    max_pool: int
    max_concurrent_cold_starts: int
    background_key: str
    stateless_prefixes: tuple[str, ...]
    close_all_concurrency: int
    drain_active_turns_timeout_secs: float
    unbind_reason_session_destroyed: str
    first_turn_nothing_armed: object
    provider_label_claude: str


@dataclass(frozen=True, slots=True)
class SessionLifecycleDeps:
    """Leaf dependencies supplied by the ``SessionManager`` facade.

    The facade should pass forwarding callables, rather than captured module
    globals, for every patch-sensitive dependency.  That keeps patches such as
    ``kiro_crew.session.build_provider_factory`` and
    ``kiro_crew.session.schedule_abort`` effective after service construction.
    """

    logger: logging.Logger
    load_config: Callable[[], Any]
    build_provider_factory: Callable[[Any], ProviderFactory]
    default_project_dir: Callable[[], str]
    constants: Callable[[], SessionLifecycleConstants]
    get_unlink_session_queue: Callable[[], Callable[[Any], None]]
    get_child_process_helpers: Callable[
        [],
        tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any]],
    ]
    get_subprocess_executor: Callable[[], Executor]
    get_platform_compat: Callable[[], Any]
    get_acp_provider_type: Callable[[], type[Any]]
    get_claude_code_provider_type: Callable[[], type[Any] | None]
    provider_label: Callable[[Any], str]
    provider_has_unfinished_turn: Callable[[Any], bool]
    provider_uses_kiro_identity_store: Callable[[Any], bool]
    get_audit_logger: Callable[[], Any]
    schedule_abort: Callable[..., None]
    monotonic: Callable[[], float]


@dataclass(slots=True)
class SessionLifecycleState:
    """Mutable state exclusively owned by :class:`SessionLifecycleService`."""

    identity_sweep_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # The fingerprint of an identity change whose sweep has not completed. Set
    # while the sweep runs and cleared only by the sweep that completes it, so an
    # outstanding change stays its own trigger for the next turn.
    identity_sweep_fingerprint: str = ""
    recycling: dict[str, _SessionEntry] = field(default_factory=dict)
    suppress_replay: set[str] = field(default_factory=set)
    origin_links: dict[str, Any] = field(default_factory=dict)
    on_recycled: _RecycleCallback | None = None
    child_teardown: _ChildTeardownHandler | None = None
    # Per-session-key count of Stop requests, keyed by folded key. Bumped by
    # :meth:`SessionLifecycleService.stop_turn` BEFORE the provider cancel is
    # awaited, so a turn runner that snapshots the count at turn start and
    # re-reads it at its end-of-turn gates sees a Stop from ANY surface that
    # reaches this session -- the dashboard, a linked channel command, a
    # transport's stop verb -- not only the one that owns the runner's slot.
    # Monotonic across ``reset``: a hard stop resets the session object, so a
    # flag on the session itself would vanish with the very turn it stopped.
    # Popped on the teardown paths that end the key's conversation for good
    # (``remove``, ``remove_if_unclaimed``, ``destroy``, the identity sweep),
    # beside the sibling per-key dicts, so a long-lived gateway does not keep
    # one entry per channel thread it ever stopped.
    stop_requests: dict[str, int] = field(default_factory=dict)
    # Canonical keys whose live session was just torn down by a turn that is
    # about to replay the same message on a successor (the transient-compaction
    # retry on the channel pipelines), each with the task that owns the replay.
    # Two things must hold across that window and cannot without a record of it:
    #
    # * A Stop landing there finds no session, and ``stop_turn`` records nothing
    #   for a key with no session -- so the replay would run a prompt the user
    #   had just stopped. While a key is here,
    #   :meth:`SessionLifecycleService.note_stop` records the Stop anyway.
    # * A NEWER message for the same key arriving there would claim the
    #   successor first, and the older message's replay would run -- and persist
    #   -- after it. While a key is here, every other task's ``get_or_create``
    #   for it waits until the gap closes, so the newer message runs after the
    #   replay exactly as it would have after an uninterrupted turn.
    #
    # The owner closes the gap only when its whole turn has settled and the
    # permit is released -- not at the successor claim: a waiter admitted then
    # would park on the successor's semaphore, and a further retry's reset would
    # pop that session from under it, stranding the message for good. The entry
    # is also dropped on the per-key teardown paths and at ``close_all``, so a
    # Stop on a key that is idle for good still records nothing and the record
    # dict still grows only with sessions that exist.
    replay_gaps: dict[str, "_ReplayGap"] = field(default_factory=dict)
    # Canonical key -> tasks whose held turn permit died with a session that
    # ``reset`` popped. ``release`` is key-only, so once a woken waiter has put a
    # successor under the key, the torn-down turn's own late release would land
    # on that successor and unlock a turn still in flight. A task recorded here
    # has exactly one such release owed; ``absorb_orphaned_release`` swallows
    # it, and ``adopt_turn`` clears the record when the same task acquires the
    # successor itself (a replay), whose permit it then legitimately releases.
    orphaned_holders: dict[str, set[asyncio.Task[Any]]] = field(default_factory=dict)


@dataclass(slots=True)
class _ReplayGap:
    """One open reset-to-reacquire window; see ``replay_gaps``."""

    owner: asyncio.Task[Any] | None
    closed: asyncio.Event = field(default_factory=asyncio.Event)


def _turn_in_flight(session: Any, *, refuse_only_on_active_turn: bool = False) -> bool:
    """Whether *session* is busy, as this caller's ``skip_if_busy`` means it.

    A held lease is the default answer, and the stricter one: it also covers a turn that has
    acquired but put no prompt in flight yet, which ``has_active_turn`` cannot see, and it is
    what a background sweep needs. A channel member holds its lease for the whole listening
    lifetime and CACHES the provider it was handed, so a sweep that tore that provider down
    would leave every later message driving a dead one with nothing to re-fetch it.

    A caller acting on an explicit user request passes ``refuse_only_on_active_turn`` and gets
    the narrower question instead: refusing a lifecycle holder on the lease alone would refuse
    it for as long as it exists, so the retry-when-idle such a caller offers could never
    succeed.
    """
    if session is None or not session.semaphore.locked():
        return False
    if not refuse_only_on_active_turn or not getattr(session, "lifecycle_lease", False):
        return True
    # The holder's own answer comes first: its turn begins when it dequeues a message, and the
    # setup before the prompt goes out is a window ``has_active_turn`` reports as idle.
    if getattr(session, "lifecycle_turn_active", False):
        return True
    provider = getattr(session, "provider", None)
    has_active_turn = getattr(provider, "has_active_turn", None)
    # An unknown provider shape keeps the strict answer: refusing a teardown is recoverable,
    # tearing down a streaming reply is not.
    return bool(has_active_turn()) if callable(has_active_turn) else True


def _provider_process_died(provider: Any) -> bool:
    """Whether *provider*'s own child process is known to have exited.

    ``is_process_alive`` is asked first and ``is_alive`` only as a fallback,
    because the two answer different questions: the first is "the process
    exists", the second folds in I/O staleness on some providers. A stalled but
    living process must not read as dead here, or the guard below force-kills a
    co-tenant's process over a slow pipe.

    Only a definite exit answers True. A provider with neither probe, or one
    whose probe raises, is unknowable, and the recoverable direction for an
    unknown is to leave the process alone: a leaked child tree is reclaimable,
    a co-tenant killed mid-turn is not.
    """
    for probe_name in ("is_process_alive", "is_alive"):
        probe = getattr(provider, probe_name, None)
        if not callable(probe):
            continue
        try:
            return not bool(probe())
        except Exception:  # noqa: BLE001 - an unreadable probe is an unknown, not a death
            return False
    return False


def _retire_pooled_runtime_of(
    provider: Any,
    *,
    log: logging.Logger | None = None,
    log_label: str = "Identity sweep",
) -> int:
    """Mark *provider*'s pooled runtime unclaimable; returns entries marked.

    Called when a credential change retires a session, so a chat starting after
    the change cannot land on the process that authenticated before it. The
    question goes to the pool through ``kiro_crew.agent_sdk``, because
    application code must not import the agent-backend layer.

    Best-effort by construction: a provider with no readable pid, a pool holding
    nothing on it, and sharing switched off all answer 0, and none of them is a
    reason to hold up the sweep.
    """
    client = getattr(provider, "_client", None)
    pid = getattr(client, "_pid", None) if client else None
    if not isinstance(pid, int):
        return 0
    log = log or logging.getLogger(__name__)
    try:
        from kiro_crew.agent_sdk import chat_runtime_retire_pid

        return chat_runtime_retire_pid(pid)
    except Exception:
        log.debug("%s: pooled runtime for PID %s not retired", log_label, pid, exc_info=True)
        return 0


def _pid_has_live_tenant(
    pid: int,
    *,
    sessions: Mapping[Any, Any],
    exclude_key: Any,
    exclude_session: Any,
    pool_tenancy: Callable[[int], bool] | None = None,
    log: logging.Logger | None = None,
    log_label: str = "Reset",
) -> bool:
    """Whether anything OTHER than the session being reset still holds a live *pid*.

    A refcounted chat runtime outlives one session's shutdown by design -- its
    co-tenants hold it and the last one to leave kills it -- so this decides
    both whether to SIGKILL a pid that outlived shutdown and whether to sweep
    its escaped children. One flag governs both, because a process worth
    keeping is a process whose children are in use.

    Two sources are unioned, because neither sees the whole picture:

    * The live session table covers a process shared for any reason at all,
      including reasons the chat runtime pool knows nothing about. But a session
      enters that table only once ``provider.start`` RETURNS, while a joining
      session takes its pool lease INSIDE start, so for the length of a cold
      start the table shows no survivor.
    * The pool's own lease count closes exactly that window, asked through
      ``kiro_crew.agent_sdk`` because application code must not import the
      agent-backend layer.

    Each source requires the process to be ALIVE, which is what keeps the
    union from having an inverse failure: a crashed runtime leaves both a
    registered session and an outstanding lease pointing at a pid that no
    longer exists, and counting either would suppress the reap and the child
    sweep of a genuinely dead process, stranding its escaped children. The
    predicate is liveness of the PROCESS, not registration of a session, which
    is why it still admits a joiner that holds a lease on a live runtime while
    being absent from the session table.
    """
    for other_key, other in list(sessions.items()):
        if other_key == exclude_key or other is exclude_session:
            continue
        other_provider = getattr(other, "provider", None)
        other_client = getattr(other_provider, "_client", None)
        other_pid = getattr(other_client, "_pid", None) if other_client else None
        if not (isinstance(other_pid, int) and other_pid == pid):
            continue
        if _provider_process_died(other_provider):
            continue
        return True
    log = log or logging.getLogger(__name__)
    if pool_tenancy is None:
        try:
            from kiro_crew.agent_sdk import chat_runtime_pid_has_tenants

            pool_tenancy = chat_runtime_pid_has_tenants
        except Exception:
            log.debug("%s: pooled tenancy for PID %s unreachable", log_label, pid, exc_info=True)
            return False
    try:
        return bool(pool_tenancy(pid))
    except Exception:
        log.debug("%s: pooled tenancy for PID %s unreadable", log_label, pid, exc_info=True)
        return False


class SessionLifecycleService:
    """Coordinate provider retirement while preserving facade dispatch seams."""

    def __init__(
        self,
        owner: SessionLifecycleOwner,
        deps: SessionLifecycleDeps,
        state: SessionLifecycleState,
    ) -> None:
        self._owner = owner
        self._deps = deps
        self.state = state

    @property
    def _identity_sweep_lock(self) -> asyncio.Lock:
        return self.state.identity_sweep_lock

    @_identity_sweep_lock.setter
    def _identity_sweep_lock(self, lock: asyncio.Lock) -> None:
        self.state.identity_sweep_lock = lock

    def stop_generation(self, key: str) -> int:
        """How many Stop requests have been recorded for *key*.

        Monotonic per folded key; 0 for a key never stopped. A turn runner
        snapshots this at turn start and treats any later change as a user
        Stop, whichever surface issued it. Recorded by :meth:`stop_turn` and by
        :meth:`note_stop`, which the channel stop paths that cancel the provider
        directly call instead.
        """
        return self.state.stop_requests.get(self._stop_bucket(key), 0)

    def _stop_bucket(self, key: str) -> str:
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

    def note_stop(self, key: str) -> bool:
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

    def open_replay_gap(self, key: str) -> None:
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

    def close_replay_gap(self, key: str) -> None:
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

    def _discard_replay_gap(self, gap_key: str) -> None:
        gap = self.state.replay_gaps.pop(gap_key, None)
        if gap is not None:
            gap.closed.set()

    def _orphan_turn_holder(self, key: str, session: Any) -> None:
        """Remember the task holding a popped session's permit; see ``orphaned_holders``."""
        holder = getattr(session, "turn_owner", None)
        if holder is None or not getattr(session, "semaphore").locked():
            return
        self.state.orphaned_holders.setdefault(canonical_key(key), set()).add(holder)

    @staticmethod
    def _calling_task() -> asyncio.Task[Any] | None:
        """The current task, or None off the loop (``release`` is also called
        synchronously from non-async code, where nothing can be orphaned)."""
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    def absorb_orphaned_release(self, key: str) -> bool:
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

    def adopt_turn(self, key: str) -> None:
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

    @staticmethod
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

    async def await_replay_gap(self, key: str) -> None:
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

    @property
    def _recycling(self) -> dict[str, _SessionEntry]:
        return self.state.recycling

    @_recycling.setter
    def _recycling(self, recycling: dict[str, _SessionEntry]) -> None:
        self.state.recycling = recycling

    @property
    def _suppress_replay(self) -> set[str]:
        return self.state.suppress_replay

    @_suppress_replay.setter
    def _suppress_replay(self, suppress_replay: set[str]) -> None:
        self.state.suppress_replay = suppress_replay

    @property
    def _origin_links(self) -> dict[str, Any]:
        return self.state.origin_links

    @_origin_links.setter
    def _origin_links(self, origin_links: dict[str, Any]) -> None:
        self.state.origin_links = origin_links

    @property
    def _on_recycled(self) -> _RecycleCallback | None:
        return self.state.on_recycled

    @_on_recycled.setter
    def _on_recycled(self, callback: _RecycleCallback | None) -> None:
        self.state.on_recycled = callback

    @property
    def _child_teardown(self) -> _ChildTeardownHandler | None:
        return self.state.child_teardown

    @_child_teardown.setter
    def _child_teardown(self, handler: _ChildTeardownHandler | None) -> None:
        self.state.child_teardown = handler

    async def refresh_defaults(self, cfg: Any = None) -> None:
        """Adopt config changes that only affect new sessions.

        ``cfg`` is an already-loaded config -- the config watcher hands in the
        one it just loaded so the apply needs no second read. ``None`` loads
        here, off-loop, for the request-handler callers.
        """
        owner = self._owner
        logger = self._deps.logger
        constants = self._deps.constants()
        async with owner._pool_fill_lock:
            # Loaded OFF the event loop, and INSIDE the fill lock. Both halves
            # are load-bearing:
            #
            # Off-loop, because load_config() stats and reads the file, validates
            # it, and deep-copies the validated dict even on a cache hit. Every
            # caller here is a request handler (a settings write, a crew write, a
            # Slack command), so that work on the loop stalls every other
            # session's turn, and a stall past
            # dashboard.loop_stall_exit_after_secs takes the gateway down.
            #
            # Inside the lock, because going off-loop introduces an await point
            # between READING the config and INSTALLING it. Two overlapping
            # refreshes could then finish their loads out of order and let the
            # older one install last, pinning every new session to stale defaults
            # until the next restart -- silently, which is the failure mode this
            # method exists to prevent. Holding the lock across both makes
            # read-then-install atomic per refresh, and costs only that
            # serialization: the load still never touches the loop.
            if cfg is None:
                cfg = await asyncio.to_thread(self._deps.load_config)
            # Same reason as the load: default_project_dir() reads the config
            # file and stats the workspace directory, so it stays off the loop
            # and outside owner._lock, which every session turn contends for.
            pool_cwd = await asyncio.to_thread(self._deps.default_project_dir)
            # Built before the lock is taken and before either owner attribute
            # is touched: if this raises, ``owner._cfg`` and
            # ``owner._provider_factory`` must still be the previous,
            # consistent pair -- not cfg swapped in with the old factory still
            # live, which is what a watcher retry would otherwise re-enter
            # against.
            provider_factory = self._deps.build_provider_factory(cfg)
            async with owner._lock:
                owner._cfg = cfg
                owner._provider_factory = provider_factory
                # The warm pool's shape is config too: size, agent, cwd and TTL
                # are captured into WarmPoolState at construction, so a refresh
                # that rebuilt the factory but left them alone kept spawning the
                # OLD pool size and agent, and the TTL was never re-adopted by
                # any path. Same clamp as WarmSessionPool._state_from_owner.
                owner._pool_size = min(constants.max_pool, max(0, cfg.session.pool_size))
                owner._pool_agent = cfg.session.pool_agent or getattr(
                    cfg.agent,
                    "default_agent",
                    "",
                )
                owner._pool_ttl_secs = max(0, cfg.session.pool_ttl_secs)
                owner._pool_cwd = pool_cwd
                while not owner._warm_pool.empty():
                    try:
                        provider, _ = owner._warm_pool.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await owner._discard_pool_provider(provider, "Default changed")
        # The health sweep returns early on an empty pool, so a drained pool
        # must be explicitly restarted with the new factory.
        owner._pool_started = False
        if owner._pool_health_task and not owner._pool_health_task.done():
            owner._pool_health_task.cancel()
            owner._pool_health_task = None
        await owner.start_pool(blocking=False)
        # Background runtimes capture the backend at spawn and must be retired
        # separately from registered providers after a backend switch.
        await owner._retire_stale_backend_bg_runtime()
        logger.info(
            "Session defaults refreshed: model=%s effort=%r (live sessions untouched)",
            cfg.agent.model,
            cfg.agent.reasoning_effort,
        )

    async def reload_provider_factory(self, cfg: Any = None) -> None:
        """Reload the provider factory and tear down providers from the old one.

        ``cfg`` is the already-loaded config the live applier hands in so the
        switch does no filesystem work on the loop; ``None`` loads it here.
        """
        owner = self._owner
        logger = self._deps.logger
        constants = self._deps.constants()
        if cfg is None:
            cfg = self._deps.load_config()
        stale: list[tuple[str, Any]] = []
        async with owner._pool_fill_lock:
            pool_cwd = await asyncio.to_thread(self._deps.default_project_dir)
            async with owner._lock:
                owner._cfg = cfg
                owner._provider_factory = self._deps.build_provider_factory(cfg)
                # The same four pool fields refresh_defaults adopts: a reset
                # handler that loads a disk-edited pool_ttl_secs must not evict
                # the warm pool at the stale TTL until the watcher's next cycle.
                owner._pool_size = min(constants.max_pool, max(0, cfg.session.pool_size))
                owner._pool_agent = cfg.session.pool_agent or getattr(
                    cfg.agent,
                    "default_agent",
                    "",
                )
                owner._pool_ttl_secs = max(0, cfg.session.pool_ttl_secs)
                owner._pool_cwd = pool_cwd
                while not owner._warm_pool.empty():
                    try:
                        provider, _ = owner._warm_pool.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await owner._discard_pool_provider(provider, "Stale pool drain")
                # Intentionally clear only the registry: the original reload
                # path does not rewrite session-map or compaction state here.
                stale = list(owner._sessions.items())
                for stale_key, _ in stale:
                    owner._advance_session_generation(stale_key)
                owner._sessions.clear()
                # Same tick as the clear. This removal had no end record, so a
                # replacement under a reused key inherited the old start and
                # reported a lifetime spanning two sessions. Recorded as one set,
                # so the awaited unlink cannot be cancelled between two keys and
                # leave the rest behind as fabricated crashes.
                await record_sessions_ended(
                    [stale_key for stale_key, _ in stale], end_reason=END_REASON_RETIRED
                )
        # Shutdown remains outside both locks. Queue unlinking and companion
        # runtime release are intentionally not added to this historical path.
        for key, sess in stale:
            try:
                await sess.provider.shutdown()
            except Exception:
                logger.debug(
                    "Failed to shut down session %s on provider switch",
                    key,
                    exc_info=True,
                )
        owner._pool_started = False
        if owner._pool_health_task and not owner._pool_health_task.done():
            owner._pool_health_task.cancel()
            owner._pool_health_task = None
        await owner.start_pool(blocking=False)
        logger.info(
            "Provider factory reloaded: provider=%s, cleared %d sessions",
            cfg.agent.provider,
            len(stale),
        )

    async def reset(
        self,
        key: str,
        *,
        expect_session: _SessionEntry | None = None,
        skip_if_busy: bool = False,
        skip_if_injecting: bool = False,
        refuse_only_on_active_turn: bool = False,
        clear_conversation: bool = False,
        ends_conversation: bool = False,
    ) -> bool:
        """Kill a live session while preserving the exact reset semantics.

        Defaults to recycling a PROCESS while the conversation survives. The session-map
        entry keeps its resume sid, so the next turn on the key restores the same native
        conversation through ``session/load`` — which is why this is the verb every
        evict-and-retry path reaches for: a wedged prompt, a failed auto-compaction, a
        provider or model switch, an idle expiry, the channel watchdog, a per-step
        re-prompt. Those must NOT stop this parent's sub-agent runs: the child has a
        conversation to deliver into and is bounded by its own run timeout, so stopping it
        would discard live work belonging to a conversation that is coming back.

        ``ends_conversation=True`` says the caller is ending the conversation, not
        recycling it, and then this parent's runs are stopped like any other parent end.
        Most endings are a different verb (``remove``, ``destroy``,
        ``discard_conversation``, ``remove_if_unclaimed``,
        ``retire_kiro_identity_sessions``), but some reach only this one, so the intent
        has to be sayable here. The callers that pass it are named in
        ``docs/system-specs/modules/session.md``; a test pins that they still do.

        The default is the recycle because that is what the overwhelming majority of the
        ~46 callers are, and the cost of the two mistakes is not symmetric -- but a MISSED
        flag costs MORE than an orphan, which is the number a new caller has to weigh. It
        arms no delivery gate either, so the child's report still reaches ``_on_done``,
        which resolves the parent through ``get_or_create`` -- the call that CREATES a
        session when none is live -- so the conversation the caller ended re-opens, seeded
        with that report. That is the headline defect in full, not a bounded process. A
        wrong ``True`` destroys live work for a conversation that comes right back, which
        is why the default stays the recycle. Nothing structural catches an omission,
        because a keyword is invisible to the AST ratchet, which is exactly why
        ``test_the_conversation_ending_reset_callers_say_so`` pins the callers BY PATH.

        ``refuse_only_on_active_turn`` narrows ``skip_if_busy`` to a DECLARED turn, which a
        caller acting on an explicit user request needs: a channel member holds its lease for
        its whole listening life, so refusing on the lease alone refuses that caller forever
        and the retry-when-idle it offers can never succeed.
        """
        owner = self._owner
        logger = self._deps.logger
        key = owner._fold_key(key)
        async with owner._lock:
            current = owner._sessions.get(key)
            if expect_session is not None and current is not expect_session:
                return False
            if skip_if_busy and _turn_in_flight(
                current, refuse_only_on_active_turn=refuse_only_on_active_turn
            ):
                return False
            # A completion injection commits a turn to this session BEFORE it
            # acquires the semaphore, so the check above cannot see one. A caller
            # that asks the counter itself still races this lock: acquiring it
            # suspends, and an injection beginning in that gap is invisible to
            # any read taken earlier. Asking again HERE is what makes the answer
            # atomic with the pop, which is why identity and the semaphore are
            # re-validated under this lock too. Opt-in, so a user-initiated reset
            # still wins over an injection. Fail closed and locally, so an
            # unreadable counter declines this reset rather than raising through
            # a sweep that has other candidates to visit.
            if skip_if_injecting:
                try:
                    injecting = owner._has_pending_injection(key)
                except Exception:
                    logger.debug(
                        "Injection probe failed for session %s; keeping it",
                        key,
                        exc_info=True,
                    )
                    injecting = True
                if injecting:
                    return False
            session = owner._sessions.pop(key, None)
            # Snapshotted in the SAME lock hold as the pop, and only when the caller says
            # the conversation is ending: every await below is a window a cold start can
            # register a successor under this key in, and a selection made after one would
            # name the successor's runs. A recycle takes no snapshot at all -- its children
            # keep running and deliver into the resumed conversation.
            teardown_children = self._snapshot_parent_children(key) if ends_conversation else ()
            owner._advance_session_generation(key)
            owner._compact_cooldown_until.pop(key, None)
            # ``clear_conversation`` means the conversation is being THROWN AWAY,
            # and the successor's replay is the one thing that can put it back.
            # Arming suppression here is what stops the reset that critical
            # context escalates to (``_reset_still_critical``, this flag's only
            # ``clear_conversation=True`` caller) from becoming a loop: the
            # successor cold-starts, ``build_session_replay`` re-injects the whole
            # conversation log as ONE prompt, a single prompt is not something
            # ``/compact`` can shrink, so the reading stays above
            # ``_POST_COMPACT_RESET_PCT`` and escalates again -- forever, since
            # the pop above also takes the cooldown that would have damped it.
            # A discard here is right for every OTHER reset (an idle expiry, a
            # provider switch, the watchdog): those keep the conversation, so
            # replaying it is the behaviour that preserves it. The manual
            # ``discard_conversation(replay=False)`` path already arms the flag
            # for exactly this reason; this branch is the one clear that forgot.
            # Gated on ``session is not None`` for the same reason the
            # ``clear_sid`` call below is: with nothing to clear there is no
            # conversation to throw away, so arming the flag would only make the
            # next cold start on an unrelated key amnesiac.
            if clear_conversation and session is not None:
                self._suppress_replay.add(key)
            else:
                self._suppress_replay.discard(key)
            owner._compact_pending_verdict.pop(key, None)
            self._origin_links.pop(key, None)
            if session is not None:
                # Order matters: the holder is recorded BEFORE its waiters are
                # woken, so the successor a woken waiter creates is already
                # shielded from the holder's late release.
                self._orphan_turn_holder(key, session)
                self._wake_turn_waiters(session)
                # Same event-loop tick as the pop, for the reason the clear_sid
                # call below documents: the awaits further down let a racing cold
                # start register a SUCCESSOR under this key, and recording the
                # end after them would consume the successor's start instead of
                # this session's. The pop and the sample happen before this call's
                # own suspension point, so that ordering still holds; only the
                # crumb unlink is deferred to a worker.
                # Append-only the session's log (flag-gated, fail-soft). Reset is
                # the teardown that ends a crew log's life, since the successor
                # cold-starts a new ACP session id. Written BEFORE the await
                # below: the emitter hands the entry to its own thread and
                # returns, so this adds no suspension point, while writing it
                # after would let a live turn's entries take a lower seq than the
                # teardown that already happened. Entries from turns that were
                # in flight still follow it -- see `on_session_closed` -- but the
                # teardown's own position stays where the decision was made.
                # Deferred, not module-scope: this module is reached from the gateway
                # boot path, and AUTOSDE's no-new-work-on-gateway-boot-path rule asks
                # for a flag-gated subsystem's IMPORT to be gated, not just its use.
                from kiro_crew.crew_log import emit as crew_log_emit

                crew_log_emit.on_session_closed(
                    crew_log_emit.session_id_of(session.provider),
                    END_REASON_RESET,
                )
                await record_session_ended(key, end_reason=END_REASON_RESET)
        if clear_conversation and session is not None:
            # The registry lock, not an absence of suspension points, is what makes
            # this safe: the end record above awaits, but it awaits while this
            # coroutine still holds ``owner._lock`` -- the lock a racing cold start
            # must take to register and map a SUCCESSOR -- so no successor SID can
            # be published across that suspension. Releasing an ``asyncio.Lock``
            # wakes its waiter without yielding, so control reaches this line before
            # any of them runs, and this clear cannot erase a successor's pointer.
            owner._session_map.clear_sid(key)
        shutdown_error: BaseException | None = None
        if session:
            await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
            # Capture PID and child tree before shutdown clears them.
            client = getattr(session.provider, "_client", None)
            raw_pid = getattr(client, "_pid", None) if client else None
            if raw_pid is None:
                cc_proc = getattr(session.provider, "_proc", None)
                if cc_proc is not None and cc_proc.returncode is None:
                    raw_pid = cc_proc.pid
            if raw_pid is None:
                cc_proc = getattr(session.provider, "_active_proc", None)
                if cc_proc is not None and cc_proc.returncode is None:
                    raw_pid = cc_proc.pid
            pid = raw_pid if isinstance(raw_pid, int) else None
            raw_children = getattr(client, "_child_pids", None) if client else None
            child_pids: dict[Any, Any] = (
                dict(raw_children) if isinstance(raw_children, dict) else {}
            )
            capture_child_records, get_child_pids, kill_escaped_children = (
                self._deps.get_child_process_helpers()
            )

            if pid:
                # Snapshot descendants before shutdown; record capture retains
                # process start times so a recycled PID is never killed later.
                loop = asyncio.get_running_loop()
                fresh = await loop.run_in_executor(
                    self._deps.get_subprocess_executor(),
                    get_child_pids,
                    pid,
                )
                new_pids = [candidate for candidate in fresh if candidate not in child_pids]
                if new_pids:
                    child_pids.update(
                        await loop.run_in_executor(
                            self._deps.get_subprocess_executor(),
                            capture_child_records,
                            new_pids,
                        )
                    )
            try:
                await session.provider.shutdown()
            except BaseException as exc:  # noqa: BLE001 - re-raised below, after the cancel
                # DEFERRED rather than propagated, and re-raised at the end of the method.
                # The cancel that ends this parent's children lives past the bottom of this
                # block, so an exception leaving here skipped it and left children running
                # with no parent to report to -- the outcome this verb exists to prevent,
                # on the one path nobody exercises. A ``finally`` around the whole block
                # would say the same thing; deferring says it without re-indenting two
                # hundred lines of kill-and-sweep logic, which is its own risk.
                shutdown_error = exc
            platform_compat = self._deps.get_platform_compat()
            if pid:
                # A process another LIVE session is still bound to is not a
                # survivor to reap. A refcounted chat runtime outlives this
                # session's shutdown by design -- its co-tenants hold it, and
                # whichever session leaves last kills it -- so SIGKILLing a pid
                # merely because it outlived one shutdown would end every other
                # session on it mid-turn, and the child sweep would take the MCP
                # servers they are using with it.
                #
                # Both sources of that answer, and why each of them requires the
                # process to be alive, are in ``_pid_has_live_tenant``.
                shared_with_others = _pid_has_live_tenant(
                    pid,
                    sessions=self._owner._sessions,
                    exclude_key=key,
                    exclude_session=session,
                    log=logger,
                    log_label=f"Reset {key}",
                )
                if shared_with_others:
                    logger.info(
                        "Reset %s: PID %d still serves other live sessions; leaving it and "
                        "its children alone",
                        key,
                        pid,
                    )
                elif platform_compat.pid_exists(pid):
                    logger.warning("Reset %s: PID %d survived shutdown, force-killing", key, pid)
                    try:
                        await platform_compat.kill_process_tree_async(
                            pid,
                            platform_compat.SIGKILL,
                        )
                    except (ProcessLookupError, OSError):
                        try:
                            await platform_compat.kill_pid_async(pid, platform_compat.SIGKILL)
                        except (ProcessLookupError, OSError):
                            pass
                if child_pids and not shared_with_others:
                    try:
                        sweep_loop = asyncio.get_running_loop()
                        await sweep_loop.run_in_executor(
                            self._deps.get_subprocess_executor(),
                            kill_escaped_children,
                            child_pids,
                        )
                    except Exception:
                        logger.exception("Reset %s: child sweep failed", key)
            logger.debug("Reset session: %s (pid=%s)", key, pid)
        # BEFORE the runtime release below, and outside the ``if session`` block above.
        #
        # Outside, because a live provider is not what makes this an ending: a RECYCLING
        # reset pops the session, so an ENDING reset that follows one arrives with
        # ``session is None`` while the children are still running -- the shape that
        # skipped the cancel in ``remove``. Gated on the INTENT rather than on the id set
        # being empty, because an empty set still reaches the durable-row sweep and a
        # recycle must not touch the store rows of a conversation that will resume.
        #
        # Before, because that is the order every other verb keeps and the helper's own
        # docstring requires: a child is stopped through its own teardown rather than by
        # having the runtime it is multiplexed onto pulled out from under a live turn.
        if ends_conversation:
            await self._cancel_parent_children(key, teardown_children, verb="reset")
        if session is not None and key in owner._subagent_runtimes:
            try:
                await owner.release_subagent_runtime(key)
            except Exception:
                logger.debug("Reset %s: subagent runtime cleanup failed", key, exc_info=True)
        if shutdown_error is not None:
            # The caller still sees what went wrong; it just sees it after this parent's
            # children have been dealt with rather than instead of that.
            raise shutdown_error
        return session is not None

    def set_recycle_callback(self, cb: _RecycleCallback | None) -> None:
        """Register the watchdog recycle notification callback."""
        if self._on_recycled is not None and cb is not None:
            self._deps.logger.warning(
                "Recycle callback already registered; replacing existing handler"
            )
        self._on_recycled = cb

    async def _fire_recycle_callback(self, key: str, *, reason: str) -> None:
        """Invoke ``_on_recycled`` if registered, swallowing exceptions."""
        callback = self._on_recycled
        if callback is None:
            return
        try:
            await callback(key, reason=reason)
        except Exception:
            self._deps.logger.exception("Recycle callback failed for %s", key)

    def set_child_teardown_handler(self, handler: _ChildTeardownHandler | None) -> None:
        """Register the two-half sub-agent teardown hook used at every parent end."""
        if self._child_teardown is not None and handler is not None:
            self._deps.logger.warning(
                "Child teardown handler already registered; replacing existing handler"
            )
        self._child_teardown = handler

    async def end_children_for(self, key: str) -> None:
        """End *key*'s sub-agent runs without touching its process.

        The conversation-ended half of a parent end, on its own. Every other verb here
        reaches it as part of tearing a session down, but a caller can replace the
        conversation on a LIVE process -- the workflow pool does exactly that when it hands
        a warm worker to the next task through ``provider.new_conversation()``. The process
        survives and the conversation does not, so the children of the conversation that
        ended have nowhere to report: without this they inject into whatever the reused
        worker is doing next.

        Same two-phase shape as the teardown verbs, for the same reason: the snapshot is
        taken under the registry lock so it cannot name the runs of a conversation that
        starts after it, and the cancel runs outside the lock because it awaits.

        No provider shutdown, no map mutation, no runtime release: this verb makes exactly
        one claim, that the conversation is over. In particular it does NOT advance the
        key's ownership generation -- that counter belongs to session allocation, the
        process here leaves the session in place, and advancing it from a verb that
        retires nothing would report a replacement to every reader of it.
        """
        owner = self._owner
        async with owner._lock:
            teardown_children = self._snapshot_parent_children(key)
        # Names ITSELF in the audit, exactly as the six teardown verbs do, rather than
        # forwarding a verb a caller supplies. One consumer passed one value, so the
        # parameter carried no information the method name does not -- and it bought the
        # verb-naming ratchet an exemption, which is machinery in place of a rule. The
        # caller's own identity is not lost: the line's ``key`` carries it.
        await self._cancel_parent_children(key, teardown_children, verb="end_children_for")

    def _snapshot_parent_children(self, key: str) -> tuple[str, ...]:
        """The runs *key* owns, read synchronously so the answer cannot drift.

        Called while this module still holds ``owner._lock``, in the same block as
        the pop. That is the only place the answer is certain: every await after it
        is a window in which a cold start can register a successor under the same
        key, and a snapshot taken after one would include the successor's runs.
        Comments throughout this file already treat that window as reachable.
        """
        handler = self._child_teardown
        if handler is None or not key:
            return ()
        try:
            return tuple(handler.snapshot_teardown_children(key))
        except Exception:
            self._deps.logger.exception("Parent end %s: snapshotting sub-agents failed", key)
            return ()

    async def _cancel_parent_children(
        self,
        key: str,
        agent_ids: "Sequence[str]",
        *,
        verb: str,
    ) -> None:
        """Stop the snapshotted runs, ahead of reaping the runtime they share.

        *key* is passed alongside the snapshot rather than instead of it, so the
        teardown's audit line can name the conversation the ids belonged to.

        Paired with :meth:`SessionManager.release_subagent_runtime` at every site
        that calls it, because that call IS this module's parent-end boundary and
        the two halves of ending a parent belong together.

        The pairing is what makes the rule backend-independent. Releasing the
        companion runtime kills the process a session-sharing child lives ON, so
        a parent end already ends the children of a harness that multiplexes —
        as a side effect of reaping the process, not as a decision. A harness
        that runs one process per child has no entry in ``_subagent_runtimes``,
        so the release reaches nothing and its children outlive the conversation
        that asked for them, each holding its own agent process and that
        process's MCP fleet until its own run timeout expires. Asking the manager
        to cancel makes the same thing happen for every harness, by intent, and
        it happens FIRST so a child is stopped through its own teardown rather
        than by having its runtime pulled out from under a live turn.

        Bounded and best-effort, matching :meth:`_fire_recycle_callback`: a
        parent end must not hang or fail on an unresponsive child, and the
        release below is the backstop for anything the cancel does not reach.

        ``close_all`` is deliberately NOT a caller. Gateway shutdown cancels
        every run at once through ``SubagentManager.cancel_all``, which also
        drains follow-up watchers and announces undelivered messages — work a
        per-key cancel does not do.
        """
        handler = self._child_teardown
        if handler is None or (not agent_ids and not key):
            return

        # The timeout bounds the PARENT'S WAIT, not the reap. A bare ``wait_for`` cancels
        # the coroutine it is waiting on, and this coroutine kills child processes: a
        # write-capable child whose reset runs long would have its ``_force_reap``
        # cancelled part-way, after the marks were written and before the kills landed,
        # leaving it executing tools against a conversation that has ended. So the reap
        # runs as its own task, shielded, and a timeout leaves it running to completion.
        #
        # The task is registered in ``_background_tasks`` for the ordinary reason: a task
        # with no strong reference can be garbage-collected mid-flight, and this one has
        # nothing else holding it once the wait gives up.
        task = asyncio.ensure_future(
            handler.cancel_for_teardown(
                agent_ids,
                parent_session_key=key,
                # Carried only so the teardown's single audit line can name WHICH verb
                # ended the conversation. Six verbs reach this one helper, and "a parent
                # end cancelled these runs" is not answerable from outside without it.
                verb=verb,
            )
        )
        self._owner._background_tasks.add(task)
        task.add_done_callback(self._owner._background_tasks.discard)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_CHILD_CANCEL_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            self._deps.logger.warning(
                "Parent end: cancelling sub-agents %s exceeded %.0fs; it continues in the "
                "background and the runtime release still runs",
                list(agent_ids),
                _CHILD_CANCEL_TIMEOUT_SECS,
            )
        except Exception:
            self._deps.logger.exception(
                "Parent end: cancelling sub-agents %s failed", list(agent_ids)
            )

    def _release_turn_ceiling(self, folded: str, requested: str) -> None:
        """Forget a channel conversation's turn-ceiling latch, beside the other
        per-key state a reset verb forgets.

        A channel conversation that reached its turn ceiling is latched under its
        key, and the key survives every one of these verbs -- channel linkage is
        retained by design. The refusal text tells the user to reset the
        conversation, so each verb the product calls a reset has to be what
        clears it: otherwise the only thing that releases a latched conversation
        is a gateway restart and the notice names a remedy that does nothing.
        Slack's ``!agent default`` answers "Reset to default agent" and reaches
        ``remove``, so a reset on one verb and not another is the same trap with a
        different spelling.

        BOTH spellings, because the ceiling counts under the key the CHANNEL holds
        while ``_fold_key`` resolves an alias onto the live key, so the two can
        differ and clearing only the folded one would leave the latch standing
        under the channel's own spelling.
        """
        ceiling = turn_ceiling.shared_ceiling()
        ceiling.reset(folded)
        if requested != folded:
            ceiling.reset(requested)

    async def remove(self, key: str) -> None:
        """Shut down a session while preserving its session-map entry."""
        owner = self._owner
        requested_key = key
        key = owner._fold_key(key)
        async with owner._lock:
            session = owner._sessions.pop(key, None)
            # Snapshot the runs this key owns in the SAME lock hold as the pop: every
            # await below is a window a cold start can register a successor under
            # this key in, and a selection made after one would name the
            # successor's runs. The cancel itself happens after the teardown.
            teardown_children = self._snapshot_parent_children(key)
            owner._advance_session_generation(key)
            owner._compact_cooldown_until.pop(key, None)
            self._suppress_replay.discard(key)
            owner._compact_pending_verdict.pop(key, None)
            self._origin_links.pop(key, None)
            self.state.stop_requests.pop(key, None)
            self._discard_replay_gap(key)
            self.state.orphaned_holders.pop(key, None)
            self._release_turn_ceiling(key, requested_key)
            if session is not None:
                # Same tick as the pop: see reset for why recording after the
                # teardown awaits would consume a successor's start.
                await record_session_ended(key, end_reason=END_REASON_REMOVED)
        # OUTSIDE the ``if session`` guard, because a live provider is not what makes this
        # a parent end. A reset pops the session and keeps the conversation, so the tab
        # close that follows arrives with ``session is None`` while the children are still
        # running -- and this is the call that ends them. Guarding on the provider skipped
        # exactly the sequence the two verbs make ordinary. It is a no-op when the snapshot
        # is empty, which is what a key this gateway never held produces.
        await self._cancel_parent_children(key, teardown_children, verb="remove")
        if session:
            await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
            await session.provider.shutdown()
            # INSIDE the guard, unlike the cancel above, and the asymmetry is this verb's
            # own contract: ``remove`` on a key it never held must touch nothing
            # (`test_missing_key_teardown_persistence_matrix[remove]` pins zero
            # `clear_sid`, zero `delete`, zero release). ``destroy`` and
            # ``discard_conversation`` release unconditionally because they answer a
            # different question -- they are told to forget the key, whether or not a
            # process is behind it. Cancelling children is safe unconditionally because
            # the snapshot answers empty for an unheld key; releasing is not, because it
            # is the observable that contract names.
            await owner.release_subagent_runtime(key)
            self._deps.logger.info("Removed session (map preserved): %s", key)

    async def flag_identity_stamp_mismatches(self, live: str) -> list[str]:
        """Mark sessions -- and retire idle companion runtimes -- whose child
        PROVABLY spawned under a different account.

        The consumer of the spawn-identity stamp
        (``kiro_prerequisite.stamp_spawn_identity``): each kiro-backed provider
        records the account the store held as its process started, and this
        compares those records against *live* -- the fresh fingerprint the turn
        gate just read. A mismatch means the child authenticated as an account
        other than the one the store currently names, even when the baseline and the interim
        latch both compare equal because no read ever observed the interim
        (the A->B->A round trip a gateway-wide baseline is inherently blind
        to).

        Flag-only for sessions: it sets the existing ``retire_on_identity_change``
        eviction flag -- the next acquire on that key reports the session
        invalid and the stale-provider path recycles it -- and never touches
        the sweep baseline, and never records anything sticky. That is the
        loop guard: a wrong observation
        here costs one targeted recycle of one session, not a
        retire-until-complete sweep, and on a healthy host every stamp equals
        *live* so this is a no-op per turn. Sessions with no stamp are skipped
        (``identity_stamp_mismatch`` refuses them), keeping every unstamped
        child on the pre-stamping protections rather than guessing.

        Companion runtimes are not sessions: the background runtime and the
        per-parent subagent runtimes hold their own kiro-backed processes,
        carry the same spawn stamp, and never appear in ``owner._sessions``,
        so the session scan cannot reach them. They have no per-session
        eviction flag either, so a PROVEN mismatch retires an idle one through
        the sweep's own idle-runtime reapers, narrowed by a predicate -- this
        gate never calls ``release_subagent_runtime`` itself, keeping those
        reapers the module's only such path outside the paired parent-end
        sites. A busy runtime is never killed mid-turn (killing live
        work is the defect this change exists to remove); instead the reapers
        DISPLACE it out of the claimable slot -- popped from the registry (or
        the ``_bg`` slot) and parked on a drain list -- so a new acquisition
        spawns a replacement under the live account rather than demuxing
        fresh sessions onto the wrong-account process, and the parked
        runtime is killed by a later pass once its in-flight work drains.
        """

        if not live:
            return []
        owner = self._owner
        flagged: list[str] = []
        async with owner._lock:
            for key, sess in owner._sessions.items():
                if sess.retire_on_identity_change:
                    continue
                provider = sess.provider
                if not self._deps.provider_uses_kiro_identity_store(provider):
                    continue
                stamp = getattr(provider, "spawn_identity", "") or getattr(
                    getattr(provider, "_runtime", None), "spawn_identity", ""
                )
                if identity_stamp_mismatch(stamp, live):
                    sess.retire_on_identity_change = True
                    flagged.append(key)
        # Drop the durable resume pointer for every flagged key, mirroring
        # the sweep's clear_sid over its invalidated keys. Without this, the
        # eviction that recycles the flagged session leaves the old sid in
        # the map: get_or_create's re-entry reads it as resume_sid and the
        # replacement child -- authenticated under the CURRENT account --
        # issues session/load on the FLAGGED account's conversation, whose
        # signed thinking blocks its provider then rejects wholesale. And
        # close_all skips retire-flagged sessions by design, so the stale
        # pointer would survive a gateway restart too. Outside owner._lock:
        # clear_sid persists to disk, and the worst interleaving (a successor
        # publishing its fresh sid between the flag and this clear) costs one
        # lost resume pointer, never a wrong-account load. Session keys only:
        # the runtime pseudo-keys appended below name processes, not map rows.
        for key in flagged:
            owner._session_map.clear_sid(key)
        # Companion runtimes, via the dedicated idle-runtime reapers rather
        # than releasing here: those reapers are the module's only
        # retirement path outside the paired parent-end sites, so the
        # parent-end ratchet stays a single-exemption contract. The predicate
        # narrows their reap to PROVEN wrong-account runtimes; busy runtimes
        # are skipped inside the reapers exactly as the sweep skips them.

        def _mismatched(runtime: object) -> bool:
            return identity_stamp_mismatch(getattr(runtime, "spawn_identity", ""), live)

        retired: list[str] = []
        await self._retire_kiro_subagent_runtimes(should_retire=_mismatched, retired=retired)
        flagged.extend(f"subagent-runtime:{parent_key}" for parent_key in retired)
        bg_retired: list[str] = []
        await self._retire_kiro_bg_runtime(
            should_retire=_mismatched,
            retired=bg_retired,
            reason="spawn identity mismatch retirement",
        )
        flagged.extend(bg_retired)
        if flagged:
            self._deps.logger.info(
                "Flagged %d holder(s) whose child spawned under a different "
                "account than the live one: %s",
                len(flagged),
                ", ".join(sorted(flagged)),
            )
        return flagged

    async def retire_kiro_identity_sessions(self, fingerprint: str = "") -> tuple[list[str], bool]:
        """Retire idle Kiro-backed processes after an identity-store change.

        The start-permit barrier is acquired before the registry scan, making
        that scan authoritative over cold starts that began before the account
        change. Busy sessions are marked for retirement on their next turn and
        keep the sweep incomplete; the session map and pending compaction
        verdicts intentionally survive.

        *fingerprint* is the live identity the caller observed. It keys the
        generation fence below, so a retry for the same pending change skips
        successors that already restarted under the new account, while a sweep
        under a different fingerprint captures afresh. Empty means the store
        could not be read; see the comment at the fence.
        """
        owner = self._owner
        logger = self._deps.logger
        constants = self._deps.constants()
        doomed: list[tuple[str, Any]] = []
        teardown_children_by_key: dict[str, tuple[str, ...]] = {}
        skipped = False
        # One sweep at a time. Two peers draining permits one-by-one could each
        # hold a partial barrier forever, preventing both finally blocks from
        # restoring cold-start capacity.
        async with self._identity_sweep_lock:
            # Before the barrier, and before any key becomes claimable: every
            # provider already queued in the warm pool authenticated as the
            # previous account, and a pool claim takes no cold-start permit, so
            # the barrier below cannot hold one back. Stamping the pool here is
            # what stops a cleared key from being handed one of those providers
            # while ``_retire_kiro_warm_pool`` (which must run outside the
            # barrier -- see ``mark_identity_epoch``) is still pending.
            owner._mark_identity_epoch()
            held = 0
            try:
                for _ in range(constants.max_concurrent_cold_starts):
                    await owner._start_sem.acquire()
                    held += 1
                async with owner._lock:
                    # An outstanding sweep is its own retirement trigger, so the
                    # pending fingerprint is recorded before anything is retired
                    # and survives until a sweep COMPLETES. Without it, a switch
                    # back to the reconciled account compares equal and the
                    # holders started under the interim one keep serving turns on
                    # its credential.
                    self.state.identity_sweep_fingerprint = fingerprint
                    # Selection and unregistering share one lock hold so a
                    # chosen idle object cannot start a turn before its pop.
                    #
                    # Every session backed by the Kiro identity store is retired,
                    # with no attempt to spare one that registered after the
                    # pending change. Telling a new-account successor from an
                    # old-account holder needs the identity each session
                    # authenticated under, and registration order does not carry
                    # it: a cold start that began before the switch registers
                    # after it, so an order-based test hands the old account a
                    # session it was asked to retire. Retiring one extra idle
                    # session costs a fresh native conversation; sparing the
                    # wrong one is the signature rejection this sweep exists to
                    # prevent.
                    retired_keys: list[str] = []
                    invalidated_keys: list[str] = []
                    for key in list(owner._sessions):
                        sess = owner._sessions[key]
                        if not self._deps.provider_uses_kiro_identity_store(sess.provider):
                            continue
                        # A pooled process outlives the sessions on it, and the
                        # credential it authenticated with is the one being
                        # retired -- so stop NEW sessions landing there before
                        # deciding what happens to this one. Asked through
                        # ``kiro_crew.agent_sdk`` because application code must
                        # not import the agent-backend layer. It matters most for
                        # the busy branch below, which can only MARK its session:
                        # that lease keeps the entry alive, and an unmarked entry
                        # is still the first compatible runtime with room.
                        _retire_pooled_runtime_of(
                            sess.provider, log=logger, log_label=f"Reset {key}"
                        )
                        if sess.semaphore.locked():
                            sess.retire_on_identity_change = True
                            invalidated_keys.append(key)
                            skipped = True
                            continue
                        del owner._sessions[key]
                        owner._advance_session_generation(key)
                        owner._compact_cooldown_until.pop(key, None)
                        self._suppress_replay.discard(key)
                        self._origin_links.pop(key, None)
                        self.state.stop_requests.pop(key, None)
                        self._discard_replay_gap(key)
                        self.state.orphaned_holders.pop(key, None)
                        retired_keys.append(key)
                        invalidated_keys.append(key)
                        teardown_children_by_key[key] = self._snapshot_parent_children(key)
                        # Do not clear _compact_pending_verdict: the identity
                        # recycle preserves that deferred verdict.
                        doomed.append((key, sess.provider))
                    # Same lock hold as the removals, not down in the shutdown
                    # loop below: that loop awaits, and a replacement session can
                    # register under a retired key while it does. Recorded as one
                    # set so the awaited unlink cannot be cancelled between two
                    # keys and leave the rest behind as fabricated crashes.
                    await record_sessions_ended(retired_keys, end_reason=END_REASON_RETIRED)
                # Drop the pointer to each identity-changed session's NATIVE
                # conversation, the same reason a provider switch drops it: the
                # account that minted it is not the account that would reload it.
                # An extended-thinking model's stored thinking blocks carry a
                # provider signature bound to the conversation they were minted
                # in, so replaying them under the new account is rejected whole
                # ("Invalid `signature` in `thinking` block") and every later turn
                # on that key fails the same way -- the recycle replaces the
                # process but the successor's ``session/load`` walks straight back
                # into the previous account's history. Only the pointer goes; the
                # conversation stays on disk under ``discarded_sid``, and the
                # dashboard transcript is a separate record that survives.
                #
                # Inside the permit barrier and after ``owner._lock`` is released:
                # every cold-start permit is still held here, so no successor can
                # publish a sid for these keys, while ``clear_sid`` persists to
                # disk and must not run under the registry lock.
                for key in invalidated_keys:
                    owner._session_map.clear_sid(key)
            finally:
                for _ in range(held):
                    owner._start_sem.release()

        retired: list[str] = []
        for key, provider in doomed:
            children = teardown_children_by_key.get(key, ())
            try:
                try:
                    await provider.shutdown()
                finally:
                    # See ``destroy``: the key was retired under the lock above, so a
                    # shutdown that raises must not skip the cancel. The outer ``except``
                    # turns a failure into a warning and leaves the key unretired, and the
                    # children are ended either way.
                    await self._cancel_parent_children(
                        key, children, verb="retire_kiro_identity_sessions"
                    )
                    await owner.release_subagent_runtime(key)
                retired.append(key)
            except Exception:
                logger.warning(
                    "Failed to retire session %s after an identity change",
                    key,
                    exc_info=True,
                )
                skipped = True

        # Warm-pool policy remains owned by the pool service; route through the
        # facade to retain direct manager monkeypatches and its fill-lock policy.
        if not await owner._retire_kiro_warm_pool():
            skipped = True
        if not await owner._retire_kiro_subagent_runtimes():
            skipped = True
        if not await owner._retire_kiro_bg_runtime():
            skipped = True
        if owner._starting_pids:
            # With every cold-start permit held above, residue here means a
            # producer bypassed the barrier; fail toward another sweep.
            skipped = True
        complete = not skipped
        if complete:
            # Only the sweep that OWNS the pending fingerprint may retire it. The
            # shutdowns above run outside ``identity_sweep_lock``, so a second
            # account change can acquire that lock meanwhile and record its own.
            # This sweep finishing its own work says nothing about that newer
            # change: clearing the marker would leave the caller free to advance
            # its baseline while the newer sweep's busy holders still serve turns
            # on the account it is retiring, with nothing left to retry from.
            if self.state.identity_sweep_fingerprint == fingerprint:
                self.state.identity_sweep_fingerprint = ""
            else:
                complete = False
        return retired, complete

    async def _retire_kiro_subagent_runtimes(
        self,
        should_retire: Callable[[object], bool] | None = None,
        retired: list[str] | None = None,
    ) -> bool:
        """Retire idle Kiro-backed companion runtimes.

        This method is the module's ONLY companion-runtime retirement path
        outside the paired parent-end sites: it reaps exclusively IDLE
        runtimes, so it has no running child to end (the fact its ratchet
        exemption in ``test_every_parent_end_release_site_ends_its_children``
        rests on). Callers that need a narrower reap -- the spawn-identity
        stamp gate retires only PROVEN wrong-account runtimes -- pass
        ``should_retire`` rather than calling ``release_subagent_runtime``
        themselves, which would widen that exemption.

        ``should_retire`` filters which idle runtimes are reaped (``None``
        retires every kiro-backed one -- identity-sweep semantics). The
        sweep-quiescence post-conditions (spawn locks held, runtimes still
        registered) only apply to the unfiltered sweep, since under a filter
        the surviving runtimes are the expected outcome, not incompleteness.
        ``retired`` collects the parent keys actually released.

        Under a filter, a runtime the predicate proves wrong-account is
        DISPLACED rather than killed, busy and idle alike: popped from the
        claimable registry (its per-parent spawn lock is dropped with it, so
        a waiter retries against the live map and spawns a replacement under
        the live account) and parked on ``_draining_subagent_runtimes`` until
        its in-flight work finishes. Leaving it registered is one hole this
        closes -- the registry is what ``get_subagent_runtime`` hands to NEW
        demuxed sessions, so "busy, catch it later" kept the wrong-account
        process claimable for the whole drain. Killing an IDLE-looking one on
        the same pass is the other: the busy probe races a claim that was
        handed the runtime but has not yet opened its init scope, so the kill
        is always deferred to the drain reap at the top of this method, which
        fires only on a later pass, only once the park grace has elapsed, and
        only when the runtime still has no active or initializing sessions
        (probes fail toward busy, preserving work). The unfiltered sweep
        keeps its skip-busy contract untouched: its completeness signal is
        what drives retries, and its busy sessions are already flagged for
        next-turn retirement.
        """
        owner = self._owner
        logger = self._deps.logger
        complete = True
        # Reap parked displaced runtimes whose work has drained. Runs on
        # every caller (the per-turn gate and the sweep), mirroring the bg
        # drain reap at the top of _retire_kiro_bg_runtime. A runtime the
        # spawn-identity gate parked keeps a kill grace on top of the busy
        # probe: the probe can read a just-claimed runtime as idle for the
        # sub-second stretch before the claim opens its init scope, and the
        # grace outlasts that window (see identity_park_grace_remaining).
        now = self._deps.monotonic()
        # Iterate a snapshot but REMOVE entries individually: the kill below
        # awaits, and a concurrent turn can park a new displaced runtime on
        # the list during that suspension. Rebuilding the list from this
        # snapshot would overwrite that park -- a live child already popped
        # from _subagent_runtimes, referenced nowhere, invisible to close_all
        # and the PID shield alike. Removing only what this pass actually
        # killed leaves concurrent parks untouched.
        for parked in list(owner._draining_subagent_runtimes):
            if identity_park_grace_remaining(parked, now) > 0.0:
                continue
            try:
                busy = parked.is_alive() and parked.has_active_or_initializing_sessions()
            except Exception:
                busy = True  # fail toward preserving work
            if busy:
                continue
            try:
                await parked.kill(expected=True, reason="drained identity displacement teardown")
                logger.info("Reaped a drained displaced subagent runtime")
            except Exception:
                logger.warning(
                    "Failed to reap a drained displaced subagent runtime; will retry",
                    exc_info=True,
                )
                continue
            try:
                owner._draining_subagent_runtimes.remove(parked)
            except ValueError:
                pass
        for parent_key in list(owner._subagent_runtimes):
            runtime = owner._subagent_runtimes.get(parent_key)
            if runtime is None or not self._deps.provider_uses_kiro_identity_store(runtime):
                continue
            if should_retire is not None:
                if not should_retire(runtime):
                    continue
                # Proven wrong-account: make it unclaimable NOW and drain it
                # in the park -- busy or idle alike. Killing on the same pass
                # that proved the mismatch is the race this closes: the idle
                # probe can read a runtime just handed to a new claim as idle
                # until the claim opens its init scope, so the kill always
                # waits for a LATER pass (plus the park grace) rather than
                # trusting one probe. Pop under the per-parent lock so an
                # in-flight get_subagent_runtime waiter re-checks the
                # canonical map instead of racing the displacement (mirrors
                # release_subagent_runtime's dance, minus the kill).
                lock = owner._subagent_runtime_locks.get(parent_key)
                if lock is not None:
                    async with lock:
                        displaced = owner._subagent_runtimes.pop(parent_key, None)
                        owner._subagent_runtime_locks.pop(parent_key, None)
                else:
                    displaced = owner._subagent_runtimes.pop(parent_key, None)
                if displaced is not None:
                    mark_identity_parked(displaced, now)
                    owner._draining_subagent_runtimes.append(displaced)
                    if retired is not None:
                        retired.append(parent_key)
                    logger.info(
                        "Parked the subagent runtime for %s to drain — "
                        "spawn identity mismatch displacement",
                        parent_key,
                    )
                continue
            if runtime.has_active_or_initializing_sessions():
                complete = False
                continue
            try:
                await owner.release_subagent_runtime(parent_key)
                if retired is not None:
                    retired.append(parent_key)
            except Exception:
                logger.warning(
                    "Failed to retire subagent runtime for %s after an identity change",
                    parent_key,
                    exc_info=True,
                )
                complete = False
        if should_retire is None:
            if any(lock.locked() for lock in owner._subagent_runtime_locks.values()):
                complete = False
            # This post-condition catches a runtime installed after the snapshot but
            # before its per-parent spawn lock was released.
            if any(
                runtime is not None and self._deps.provider_uses_kiro_identity_store(runtime)
                for runtime in list(owner._subagent_runtimes.values())
            ):
                complete = False
            # A parked displaced runtime is still a live process authenticated
            # under the old account; the sweep is not done until it drains.
            if any(
                self._deps.provider_uses_kiro_identity_store(runtime)
                for runtime in owner._draining_subagent_runtimes
            ):
                complete = False
        return complete

    async def _retire_kiro_bg_runtime(
        self,
        should_retire: Callable[[object], bool] | None = None,
        retired: list[str] | None = None,
        reason: str = "deliberate logout teardown",
    ) -> bool:
        """Retire the idle Kiro-backed background runtime and drained holders.

        ``should_retire`` filters the reap the same way as
        :meth:`_retire_kiro_subagent_runtimes` (``None`` retires
        unconditionally -- identity-sweep semantics); ``retired`` collects
        ``"background-runtime"`` when the kill lands; ``reason`` labels the
        kill for the process record.
        """
        owner = self._owner
        logger = self._deps.logger
        async with owner._bg_runtime_lock:
            await owner._reap_drained_bg_runtimes_locked()
            complete = not any(
                self._deps.provider_uses_kiro_identity_store(runtime)
                for runtime in owner._draining_bg_runtimes
            )
            runtime = owner._bg_runtime
            if runtime is None or not self._deps.provider_uses_kiro_identity_store(runtime):
                return complete
            if should_retire is not None and not should_retire(runtime):
                return complete
            if should_retire is not None:
                # Proven wrong-account: free the slot NOW so the next
                # acquisition spawns under the live account, and park the
                # runtime on the existing drain list -- busy or idle alike.
                # Skipping a busy one is the hole this closes (``_bg_runtime``
                # is the claimable slot, so "busy, catch it later" kept the
                # wrong-account process serving NEW background sessions for
                # the whole drain), and killing an idle-looking one on this
                # same pass is the race it avoids: the busy probe can read a
                # runtime just handed to a ``get_bg_session`` claim as idle
                # until the claim opens its init scope, so the kill is
                # deferred to the drain reap, which fires on a later pass
                # once the park grace has elapsed.
                mark_identity_parked(runtime, self._deps.monotonic())
                await owner._detach_bg_runtime_locked(
                    runtime, "spawn identity mismatch displacement", park_only=True
                )
                if retired is not None:
                    retired.append("background-runtime")
                return False
            if runtime.has_active_or_initializing_sessions():
                return False
            try:
                await runtime.kill(expected=True, reason=reason)
            except Exception:
                logger.warning(
                    "Failed to retire the background runtime after an identity change",
                    exc_info=True,
                )
                return False
            # Clear only after kill succeeds; otherwise retain the live-process
            # reference for the next retirement attempt.
            owner._bg_runtime = None
            if retired is not None:
                retired.append("background-runtime")
            logger.info("Retired the background runtime started under the previous account")
            return complete

    async def remove_if_unclaimed(self, key: str) -> bool:
        """Remove a speculative session only while its first turn is unclaimed."""
        owner = self._owner
        constants = self._deps.constants()
        requested_key = key
        key = owner._fold_key(key)
        async with owner._lock:
            session = owner._sessions.get(key)
            if (
                session is None
                or session.first_turn is constants.first_turn_nothing_armed
                or session.semaphore.locked()
            ):
                return False
            del owner._sessions[key]
            # Snapshot the runs this key owns in the SAME lock hold as the pop: every
            # await below is a window a cold start can register a successor under
            # this key in, and a selection made after one would name the
            # successor's runs. The cancel itself happens after the teardown.
            teardown_children = self._snapshot_parent_children(key)
            owner._advance_session_generation(key)
            owner._compact_cooldown_until.pop(key, None)
            self._suppress_replay.discard(key)
            owner._compact_pending_verdict.pop(key, None)
            self._origin_links.pop(key, None)
            self.state.stop_requests.pop(key, None)
            self._discard_replay_gap(key)
            self.state.orphaned_holders.pop(key, None)
            self._release_turn_ceiling(key, requested_key)
            # Same tick as the removal: see reset.
            await record_session_ended(key, end_reason=END_REASON_UNCLAIMED)
        await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
        try:
            await session.provider.shutdown()
        finally:
            # See ``destroy``: the entry is already gone, so a shutdown that raises must
            # not carry the exception past the cancel and leave orphaned children behind.
            await self._cancel_parent_children(key, teardown_children, verb="remove_if_unclaimed")
            await owner.release_subagent_runtime(key)
        self._deps.logger.info(
            "Removed unclaimed speculative session (map preserved): %s",
            key,
        )
        return True

    async def destroy(
        self,
        key: str,
        *,
        should_destroy: Callable[[], bool] | None = None,
        expect_generation: object = _ANY_SESSION,
        skip_if_busy: bool = False,
        preserve_autocompact_override: bool = False,
    ) -> bool:
        """Destroy *key* only while every synchronous under-lock guard allows it."""
        owner = self._owner
        constants = self._deps.constants()
        requested_key = key
        async with owner._lock:
            key = owner._fold_key(key)
            if expect_generation is not _ANY_SESSION and owner._has_allocation_reservation(key):
                return False
            current = owner._sessions.get(key)
            if (
                expect_generation is not _ANY_SESSION
                and owner.session_generation(key) != expect_generation
            ):
                return False
            if skip_if_busy and current is not None and current.semaphore.locked():
                return False
            if should_destroy is not None:
                try:
                    allowed = should_destroy()
                except Exception:
                    self._deps.logger.warning(
                        "Conditional session destroy guard failed for %s; skipping",
                        key,
                        exc_info=True,
                    )
                    return False
                if not allowed:
                    return False
            session = owner._sessions.pop(key, None)
            # Snapshot the runs this key owns in the SAME lock hold as the pop: every
            # await below is a window a cold start can register a successor under
            # this key in, and a selection made after one would name the
            # successor's runs. The cancel itself happens after the teardown.
            teardown_children = self._snapshot_parent_children(key)
            owner._advance_session_generation(key)
            owner._compact_cooldown_until.pop(key, None)
            self._suppress_replay.discard(key)
            owner._compact_pending_verdict.pop(key, None)
            self.state.stop_requests.pop(key, None)
            self._discard_replay_gap(key)
            self.state.orphaned_holders.pop(key, None)
            self._release_turn_ceiling(key, requested_key)
            # Ordinary permanent destroy starts a new conversation on reuse and
            # therefore clears the old threshold. History deletion can race a
            # same-key transcript claim in another process, so its explicit
            # conditional mode preserves this independently owned sidecar.
            if not preserve_autocompact_override:
                owner.set_autocompact_pct(key, None)
            # _origin_links deliberately survives destroy; existing callers
            # rely on the historical asymmetry with reset/remove.
            # The map delete is the destructive persistence linearization point.
            # It must run before record_session_ended can suspend: dashboard slot
            # publication does not take this registry lock and could otherwise
            # adopt the predecessor's still-visible binding during that await.
            owner._session_map.delete(
                key,
                reason=constants.unbind_reason_session_destroyed,
            )
            if session is not None:
                # Append-only the session's log (flag-gated, fail-soft). Destroy is a
                # teardown and has to record itself, for the same reason the reset
                # route above does -- and for one more: without this entry the
                # unit is never collected by EITHER half of retention. The emitter
                # holds this session's cached handle, and the write lease that
                # handle carries, so a removal claiming the lease `sole` answers
                # `owned`; the sweep is blocked from the other side, because a unit
                # whose newest lifecycle entry is not a close reads as OPEN
                # whatever its age. One missing entry, both paths defeated.
                #
                # The REASON asserts how far the teardown got, and retention treats
                # only `destroyed` as authorization to delete the unit's history.
                # That word means the ACP id is globally revoked, so it is claimed
                # only after checking that no OTHER key still maps to this sid: the
                # map delete above removes exactly ONE key, and a second key
                # pointing at the same sid still resolves it, which keeps the
                # conversation resumable and its log needed. Two keys on one sid is
                # a state the system itself produces -- importing a transferred
                # session twice allocates a new slot key each time and deliberately
                # leaves the source intact (see dashboard/session_transfer.py) --
                # so this is not only an adversarial shape and the other holder must
                # not be revoked to make this record tidy.
                #
                # Reading the map to WITHHOLD the claim is safe in a way that
                # reading it to grant one is not: a forged or emptied map can only
                # make this assert less than the truth, never more, and the sweep
                # itself still reads nothing but the ledger.
                #
                # Placed here, before the await below, for the reason the reset
                # site documents: the emitter hands the entry to its own thread and
                # returns, so this adds no suspension point, while writing it after
                # the await would let a live turn's entries take a lower seq than
                # the teardown that already happened. Entries from turns that were
                # in flight still follow it by design -- see `on_session_closed`.
                #
                # It cannot start a crew log for a session that has none: `_handle`
                # never creates one, so a close for an unopened session writes
                # nothing rather than leaving a header behind for a conversation
                # that is being destroyed.
                #
                # Deferred, not module-scope: this module is reached from the
                # gateway boot path, and AUTOSDE's no-new-work-on-gateway-boot-path
                # rule asks for a flag-gated subsystem's IMPORT to be gated too.
                from kiro_crew.crew_log import emit as crew_log_emit

                crew_log_sid = crew_log_emit.session_id_of(session.provider)
                retained_key: str | None = None
                if crew_log_sid:
                    try:
                        retained_key = owner._session_map.find_key_by_sid(crew_log_sid)
                    except Exception:
                        # An unreadable map is not evidence that the id is revoked.
                        retained_key = _SID_RETENTION_UNKNOWN
                if retained_key is not None:
                    self._deps.logger.info(
                        "Session destroy: %s still maps to session id of %s; recording a "
                        "non-terminal teardown so its crew log is retained",
                        retained_key,
                        key,
                    )
                crew_log_emit.on_session_closed(
                    crew_log_sid,
                    END_REASON_DESTROYED if retained_key is None else _END_REASON_SID_RETAINED,
                )
                # Still under the same lock as the pop; a manager successor cannot
                # register until its predecessor's end record is sampled.
                await record_session_ended(key, end_reason=END_REASON_DESTROYED)
        try:
            if session:
                await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
                await session.provider.shutdown()
        finally:
            # In the FINALLY, because the parent has already been removed by the time the
            # provider is asked to stop. A shutdown that raises must not carry the
            # exception past both of these: that leaves children running with no parent to
            # report to and the runtime they share still held -- the one outcome this verb
            # exists to prevent, reached by the one path nobody exercises. Cancel first,
            # then release: a child is stopped through its own teardown rather than by
            # having the runtime pulled out from under a live turn.
            await self._cancel_parent_children(key, teardown_children, verb="destroy")
            await owner.release_subagent_runtime(key)
            self._deps.logger.info("Destroyed session (map deleted): %s", key)
        return True

    async def destroy_if(
        self,
        key: str,
        expected_generation: int,
        should_destroy: Callable[[], bool],
        *,
        preserve_autocompact_override: bool = False,
    ) -> bool:
        """Destroy the captured idle generation if its slot guard stays true."""
        return await self.destroy(
            key,
            should_destroy=should_destroy,
            expect_generation=expected_generation,
            skip_if_busy=True,
            preserve_autocompact_override=preserve_autocompact_override,
        )

    async def discard_conversation(
        self,
        key: str,
        *,
        replay: bool = True,
        skip_if_busy: bool = False,
        refuse_only_on_active_turn: bool = False,
    ) -> bool:
        """Drop only the native conversation while preserving channel linkage.

        Returns whether a session was actually torn down. False means
        ``skip_if_busy`` made it a no-op; nothing was changed, including the
        replay flag and the session map.

        ``skip_if_busy`` refuses the teardown when the session has a turn in
        flight, and is enforced HERE, atomically with the pop, for the same
        reason :meth:`reset` enforces its own: a caller that probes busy-ness
        first and calls this second leaves a window between the two in which a
        turn can be admitted — a channel message acquiring the session's
        semaphore, say — and the teardown then removes the provider from under a
        reply that has started. The probe is the SEMAPHORE rather than
        ``provider.has_active_turn()``, which is deliberately stricter: a turn
        that holds the semaphore but has not yet put a prompt in flight is
        invisible to ``has_active_turn`` and is exactly the case a caller-side
        pre-check cannot close.

        The sid clear runs in the SAME event-loop tick as the pop, with no await
        between them, so a cold start racing this teardown cannot have mapped a
        replacement sid for the key by the time it runs — the clear can never
        erase a successor's pointer. Clearing it after the shutdown awaits would
        do exactly that, since the shutdown is the window a concurrent channel
        turn needs to create and map a new session under the same key.
        """
        owner = self._owner
        requested_key = key
        key = owner._fold_key(key)
        async with owner._lock:
            current = owner._sessions.get(key)
            if skip_if_busy and _turn_in_flight(
                current, refuse_only_on_active_turn=refuse_only_on_active_turn
            ):
                return False
            session = owner._sessions.pop(key, None)
            # Snapshot the runs this key owns in the SAME lock hold as the pop: every
            # await below is a window a cold start can register a successor under
            # this key in, and a selection made after one would name the
            # successor's runs. The cancel itself happens after the teardown.
            teardown_children = self._snapshot_parent_children(key)
            owner._advance_session_generation(key)
            owner._compact_cooldown_until.pop(key, None)
            owner._compact_pending_verdict.pop(key, None)
            self._release_turn_ceiling(key, requested_key)
            # Store replay suppression atomically with the pop. Origin-link
            # state intentionally survives this operation.
            if replay:
                self._suppress_replay.discard(key)
            else:
                self._suppress_replay.add(key)
            if session is not None:
                # Same lock hold as the pop, exactly like the clear_sid below.
                await record_session_ended(key, end_reason=END_REASON_DISCARDED)
        # The registry lock, not an absence of suspension points, is what keeps a
        # cold start racing this teardown from registering a replacement sid for the
        # key in between, so this clear cannot erase a SUCCESSOR's pointer. The end
        # record above awaits, but it does so while ``owner._lock`` is still held --
        # the lock that cold start must take to register and map a successor -- and
        # releasing an ``asyncio.Lock`` wakes its waiter without yielding, so control
        # reaches this line before any of them runs.
        # Deferring it past the shutdown awaits below is exactly that bug: the
        # provider shutdown is slow, a concurrent channel turn creates and maps a
        # new session under the same key while it runs, and a clear in the
        # ``finally`` then wipes the new session's sid. Mirrors ``reset``'s
        # ``clear_conversation``, which clears in this same position for this
        # same reason. Outside the lock rather than inside it because
        # ``clear_sid`` persists to disk, and the lock must not span blocking IO.
        owner._session_map.clear_sid(key)
        try:
            if session:
                await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
                await session.provider.shutdown()
        finally:
            # See ``destroy``: in the finally because a shutdown that raises must not
            # carry the exception past the cancel, and cancel before release.
            await self._cancel_parent_children(key, teardown_children, verb="discard_conversation")
            await owner.release_subagent_runtime(key)
            self._deps.logger.info(
                "Discarded native conversation (sid cleared, map entry kept): %s",
                key,
            )
        return True

    async def drain_active_turns(self, timeout: float | None = None) -> int:
        """Bring unfinished native turns to a safe boundary before teardown."""
        owner = self._owner
        logger = self._deps.logger
        if timeout is None:
            timeout = self._deps.constants().drain_active_turns_timeout_secs
        if timeout <= 0:
            return 0

        async with owner._lock:
            providers = [session.provider for session in owner._sessions.values()]
        # Already-cancelled turns can report inactive before the native done ack;
        # unfinished is the signal that the native lock may still be held.
        unfinished = [
            provider for provider in providers if self._deps.provider_has_unfinished_turn(provider)
        ]
        if not unfinished:
            return 0

        logger.info(
            "Draining %d unfinished turn(s) to a safe boundary before teardown (<= %.1fs)",
            len(unfinished),
            timeout,
        )

        async def _drain_one(provider: Any) -> None:
            cancel_fn = getattr(provider, "cancel", None)
            if not callable(cancel_fn):
                return
            try:
                outcome = await cancel_fn(wait_ack_timeout=timeout)
            except Exception:
                logger.debug("drain_active_turns: cancel failed", exc_info=True)
                return
            if outcome == "no_turn" and self._deps.provider_has_unfinished_turn(provider):
                waiter = getattr(provider, "wait_turn_done", None)
                if callable(waiter):
                    try:
                        await waiter(timeout=timeout)
                    except asyncio.TimeoutError:
                        logger.debug("drain_active_turns: post-cancel wait_turn_done timed out")
                    except Exception:
                        logger.debug(
                            "drain_active_turns: wait_turn_done failed",
                            exc_info=True,
                        )

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *[_drain_one(provider) for provider in unfinished],
                    return_exceptions=True,
                ),
                # Slightly exceed each cancel budget so its own timeout resolves
                # before the gather is cancelled.
                timeout=timeout + 1.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "drain_active_turns: %d turn(s) did not reach a safe boundary within "
                "%.1fs — proceeding to kill (kiro-cli SIGTERM grace still applies)",
                len(unfinished),
                timeout,
            )
        return len(unfinished)

    async def close_all(self, drain_timeout: float | None = None) -> None:
        """Shut down every session after a bounded cooperative turn drain."""
        owner = self._owner
        logger = self._deps.logger
        constants = self._deps.constants()
        # Enter closing under the registry lock before taking the drain
        # snapshot. This prevents a new prompt or provider registration from
        # landing in the multi-second window after that snapshot.
        async with owner._lock:
            owner._closing = True
            owner._update_pause_owned = False
        # Nothing will claim a successor once closing; release anyone waiting
        # behind a replay gap so the shutdown does not strand their task (their
        # claim then meets the closing refusal like any other).
        for gap_key in list(self.state.replay_gaps):
            self._discard_replay_gap(gap_key)

        try:
            await owner.drain_active_turns(timeout=drain_timeout)
        except Exception:
            # CancelledError is intentionally not caught: callers use an outer
            # wait_for deadline as the hard restart cap.
            logger.debug("close_all: drain_active_turns failed", exc_info=True)

        if owner._cleanup_task:
            owner._cleanup_task.cancel()

        # Pool-health and spawn tasks are registered in the same owned-task set.
        for task in list(owner._background_tasks):
            task.cancel()
        if owner._background_tasks:
            await asyncio.gather(*owner._background_tasks, return_exceptions=True)
            owner._background_tasks.clear()

        # Detach both background-runtime holders under their creation lock.
        # Killing the snapshot outside it prevents a wedged process teardown
        # from blocking every later observer of that boundary.
        async with owner._bg_runtime_lock:
            bg_doomed = [
                runtime
                for runtime in (owner._bg_runtime, *owner._draining_bg_runtimes)
                if runtime is not None
            ]
            owner._bg_runtime = None
            owner._draining_bg_runtimes = []
        for bg_runtime in bg_doomed:
            try:
                await bg_runtime.kill(expected=True, reason="graceful shutdown")
            except Exception:
                logger.debug("close_all: _bg runtime kill failed", exc_info=True)
        for key in list(owner._subagent_runtimes):
            try:
                await owner.release_subagent_runtime(key)
            except Exception:
                logger.debug(
                    "close_all: subagent runtime cleanup failed for %s",
                    key,
                    exc_info=True,
                )
        # Parked displaced runtimes are live processes outside the registry;
        # shutdown ends them like every other holder.
        parked_doomed = list(owner._draining_subagent_runtimes)
        owner._draining_subagent_runtimes[:] = []
        for parked in parked_doomed:
            try:
                await parked.kill(expected=True, reason="graceful shutdown")
            except Exception:
                logger.debug("close_all: displaced subagent runtime cleanup failed", exc_info=True)

        # Drain queued warm providers. This intentionally does not call the
        # public pool drain helper, whose informational log is not present on
        # the close_all path.
        pool_providers: list[Any] = []
        while not owner._warm_pool.empty():
            try:
                provider, _ = owner._warm_pool.get_nowait()
                pool_providers.append(provider)
            except asyncio.QueueEmpty:
                break

        async with owner._lock:
            acp_provider_type = self._deps.get_acp_provider_type()
            claude_code_provider_type = self._deps.get_claude_code_provider_type()
            for key, sess in owner._sessions.items():
                # The identity sweep already moved this old-account pointer to
                # discarded_sid. Do not map the live child's sid back at shutdown.
                if sess.retire_on_identity_change:
                    continue
                cwd_str = sess.provider.cwd
                if isinstance(sess.provider, acp_provider_type):
                    sid = sess.provider.client._session_id
                    # A replay-pending fresh child is not yet the durable
                    # conversation. Allocation retained the prior full-history
                    # SID; shutdown must not overwrite it before the replay
                    # settlement in chat_runner commits the fresh transcript.
                    if (
                        sid
                        and not sess.provider_switch_replay
                        and key != constants.background_key
                        and (
                            not any(
                                key.startswith(prefix) for prefix in constants.stateless_prefixes
                            )
                            or owner._is_continuable_key(key)
                        )
                    ):
                        provider_label = self._deps.provider_label(sess.provider)
                        owner._session_map.set(
                            key,
                            sid,
                            provider=provider_label,
                            cwd=cwd_str,
                        )
                elif claude_code_provider_type is not None and isinstance(
                    sess.provider,
                    claude_code_provider_type,
                ):
                    sid = sess.provider.session_id
                    if (
                        sid
                        and key != constants.background_key
                        and (
                            not any(
                                key.startswith(prefix) for prefix in constants.stateless_prefixes
                            )
                            or owner._is_continuable_key(key)
                        )
                    ):
                        owner._session_map.set(
                            key,
                            sid,
                            provider=constants.provider_label_claude,
                            cwd=cwd_str,
                        )

            # set() defers disk writes. aclose() is the durability point before
            # restart paths that terminate with os._exit.
            try:
                await owner._session_map.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("close_all: session map flush failed", exc_info=True)

            sessions = dict(owner._sessions)
            for session_key in sessions:
                owner._advance_session_generation(session_key)
            owner._sessions.clear()
            owner._compact_cooldown_until.clear()
            self._suppress_replay.clear()
            owner._compact_pending_verdict.clear()
            # Same lock hold as the clear: the whole drained set is accounted for
            # in one call, so the awaited unlink cannot be cancelled between two
            # keys. Per-key awaits would leave every key after the cancellation
            # popped but unrecorded -- and shutdown is precisely the path a
            # cancellation reaches, so that would turn one orderly close into a
            # burst of sessions the next boot reports as crashed.
            await record_sessions_ended(sessions, end_reason=END_REASON_SHUTDOWN)

        # Provider shutdown can enqueue multiple blocking process-maintenance
        # jobs, so keep the original bounded fan-out.
        close_sem = asyncio.Semaphore(constants.close_all_concurrency)

        async def _close_one(provider: Any) -> None:
            async with close_sem:
                try:
                    await provider.shutdown()
                except Exception:
                    pass

        all_providers = [session.provider for session in sessions.values()] + pool_providers
        if not all_providers:
            return

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *[_close_one(provider) for provider in all_providers],
                    return_exceptions=True,
                ),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Timeout closing %d sessions — orphan cleanup at next startup",
                len(all_providers),
            )
        logger.info("All sessions closed (active=%d)", len(sessions))

    async def cancel_current(
        self,
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

    async def stop_turn(
        self,
        key: str,
        *,
        force: bool = False,
        preserve_queue: bool = False,
        on_soft: Callable[[], Awaitable[None]] | None = None,
        on_hard: Callable[[], Awaitable[None]] | None = None,
    ) -> StopOutcome:
        """Cooperatively stop a turn, escalating to reset and eager respawn."""
        owner = self._owner
        logger = self._deps.logger
        key = owner._fold_key(key)
        session = owner._sessions.get(key)
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
            owner.clear_queue(key)
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

    async def _send_abort_for_session(self, key: str, session: Any) -> None:
        """Best-effort gateway abort for a session's runtime process."""
        logger = self._deps.logger
        try:
            pid, socket_path = session.provider.runtime_info()

            if pid is None:
                client = getattr(session.provider, "_client", None)
                pid = getattr(client, "_pid", None) if client else None
            if socket_path is None:
                client = getattr(session.provider, "_client", None)
                socket_path = getattr(client, "_mcp_gateway_socket", None) if client else None

            if isinstance(pid, int) and pid > 1 and socket_path:
                # Audit at the decision point: downstream logging happens only
                # if the fire-and-forget gateway abort eventually succeeds.
                try:
                    self._deps.get_audit_logger().log_api_access(
                        caller="session",
                        operation="mcp-gateway.abort-initiated",
                        outcome="initiated",
                        source="session",
                        resources=f"pid={pid} session={key}",
                        error="reason=hard-stop",
                    )
                except Exception:  # pragma: no cover - audit cannot block kill
                    logger.debug("SEL audit for abort initiation failed", exc_info=True)
                self._deps.schedule_abort(
                    socket_path,
                    [pid],
                    reason=f"hard-stop session={key}",
                )
            else:
                logger.warning(
                    "abort-push skipped for %s: no runtime pid/socket resolved "
                    "(pid=%r socket=%r) — in-flight tool calls will not be cancelled",
                    key,
                    pid,
                    socket_path,
                )
        except Exception:
            logger.debug("_send_abort_for_session failed for %s", key, exc_info=True)

    async def _eager_respawn(self, key: str) -> None:
        """Respawn after hard kill and release its acquired turn semaphore."""
        try:
            await self._owner.get_or_create(key)
            self._owner.release(key)
        except Exception:
            self._deps.logger.debug("Eager respawn failed for %s", key, exc_info=True)

    async def drain_all_providers(self) -> list[Any]:
        """Pop every registered session and return its providers.

        Records an end per popped key even though the one current caller drains
        an already-empty registry (it calls ``reload_provider_factory`` first,
        which clears and records). An unrecorded mass pop here would not lose
        samples, it would manufacture crashes: every crumb left behind is
        reported as ``crashed`` at the next boot.
        """
        owner = self._owner
        providers: list[Any] = []
        popped: list[_SessionEntry] = []
        async with owner._lock:
            keys = list(owner._sessions.keys())
            ended: list[str] = []
            for key in keys:
                session = owner._sessions.pop(key, None)
                owner._advance_session_generation(key)
                if session:
                    providers.append(session.provider)
                    popped.append(session)
                    ended.append(key)
            # One call for the whole set, so a cancellation cannot land between two
            # keys and leave the rest of them behind as fabricated crashes.
            await record_sessions_ended(ended, end_reason=END_REASON_SHUTDOWN)
        # Filesystem unlink stays outside the registry lock.
        for session in popped:
            await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
        return providers


__all__ = [
    "CancelOutcome",
    "SessionLifecycleConstants",
    "SessionLifecycleDeps",
    "SessionLifecycleService",
    "SessionLifecycleState",
    "StopOutcome",
]
