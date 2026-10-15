"""Session teardown and identity-retirement boundary.

This leaf module owns the lifecycle state that survives across individual
provider objects, while the ``SessionManager`` facade remains the authority for
session allocation, warm-pool state, background runtimes, compaction policy,
and persistence.  Cross-boundary calls deliberately route through ``owner`` so
existing instance monkeypatch seams remain observable after wiring.

There is no runtime import of :mod:`kiro_crew.session`.  Patchable module
globals, provider types, process helpers, and policy constants are resolved by
call-time dependencies supplied by the facade.

``SessionLifecycleService`` is defined here and binds its members from the
``session_lifecycle_parts`` owner modules, one responsibility each; the module-level
stop-decline and parked-queue records live there too, and every module-level name of
the lifecycle service resolves here as well (``_facade_mirror``).
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import logging
import sys as _sys
import time  # noqa: F401
from collections import OrderedDict  # noqa: F401
from collections.abc import Awaitable, Callable, MutableMapping, Sequence  # noqa: F401
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AbstractSet, Any, Literal, Protocol  # noqa: F401

from kiro_crew import _facade_mirror
from kiro_crew.kiro_prerequisite import (  # noqa: F401
    identity_component_dropped,
    identity_park_grace_remaining,
    identity_stamp_mismatch,
    mark_identity_parked,
    spawn_identity_of,
    spawned_under,
)
from kiro_crew.messaging import turn_ceiling  # noqa: F401
from kiro_crew.messaging.link import canonical_key  # noqa: F401
from kiro_crew.messaging.queue_drain import entry_channel, wake_other_drains  # noqa: F401
from kiro_crew.metrics.sessions import (  # noqa: F401
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
from kiro_crew.process_identity import ProcessHandle, process_handle_of
from kiro_crew.session_lifecycle_parts import fleet as _fleet
from kiro_crew.session_lifecycle_parts import identity_retirement as _identity_retirement
from kiro_crew.session_lifecycle_parts import parent_end as _parent_end
from kiro_crew.session_lifecycle_parts import teardown as _teardown
from kiro_crew.session_lifecycle_parts import turn_stop as _turn_stop
from kiro_crew.session_lifecycle_parts.parent_end import (  # noqa: F401
    _CHILD_CANCEL_TIMEOUT_SECS,
)
from kiro_crew.session_lifecycle_parts.parked_queue import (  # noqa: F401
    PARKED_ENTRY_KWARGS_MAX_CHARS,
    PARKED_ENTRY_TEXT_MAX_CHARS,
    PARKED_ENTRY_TS_MAX_CHARS,
    PARKED_QUEUE_MAX,
    PARKED_QUEUE_TOTAL_MAX,
    _drop_parked,
    _entry_within_bounds,
    _folded,
    _has_session,
    _park_queue,
    _parked_for,
    _parked_queue,
    _parked_total,
    adopt_parked_queue,
    allocation_identity,
    force_stop_keeping_others,
    hand_queue_to_successor,
    take_parked_queue,
)
from kiro_crew.session_lifecycle_parts.stop_decline import (  # noqa: F401
    STOP_DECLINED_COMPACTING_TEXT,
    STOP_DECLINED_ESCALATION_SECS,
    STOP_DECLINED_KEY_MAX_CHARS,
    STOP_DECLINED_MARKERS_MAX,
    StopOutcome,
    _compaction_waits_for_turn,
    _count_stop_declined_drop,
    _logger,
    _stop_declined_key,
    _stop_declined_markers,
    clear_stop_declined,
    compaction_in_flight,
    consume_stop_declined,
    decline_stop,
    note_stop_declined,
)
from kiro_crew.session_lifecycle_parts.teardown import (  # noqa: F401
    _ANY_SESSION,
    _END_REASON_SID_RETAINED,
    _SID_RETENTION_UNKNOWN,
    _turn_in_flight,
)
from kiro_crew.session_lifecycle_parts.turn_stop import (
    CancelOutcome,
    _ReplayGap,
)
from kiro_crew.session_pool import pool_kiro_agent  # noqa: F401
from kiro_crew.start_priority import PrioritySemaphore

ProviderFactory = Callable[..., Any]


class _RecycleCallback(Protocol):
    async def __call__(self, key: str, *, reason: str) -> None: ...


class _ChildTeardownHandler(Protocol):
    """Ends the sub-agent runs a parent spawned, in two halves.

    Satisfied by ``SubagentManager``. The halves are separate because they must
    run at different moments: the snapshot while this module still holds the
    registry lock, the cancel after the provider teardown awaits.
    """

    def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]: ...

    def snapshot_teardown_children_only(self, parent_session_key: str) -> tuple[str, ...]: ...

    def has_live_or_queued_children(self, parent_session_key: str) -> bool: ...

    def has_in_memory_pending_work_for(
        self, parent_session_key: str, *, exclude_id: str = ""
    ) -> bool: ...

    async def has_pending_work_for_async(self, parent_session_key: str) -> bool: ...

    def has_unfinished_report_for(self, parent_session_key: str) -> bool: ...

    async def has_pending_store_children(self, parent_session_key: str) -> bool: ...

    def open_teardown_fence(self, parent_session_key: str) -> "AbstractSet[str] | None": ...

    def has_fenced_children(self, parent_session_key: str) -> bool: ...

    def commit_identity_sweep_retire(
        self, parent_session_key: str, expected: "AbstractSet[str]"
    ) -> bool: ...

    def release_teardown_fence(
        self, parent_session_key: str, expected: "AbstractSet[str] | None" = None
    ) -> None: ...

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
    turn_owner: Any
    queue: Any
    cancelled: set[str]
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
    _start_sem: PrioritySemaphore
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

    async def release_subagent_runtime(
        self, parent_session_key: str, *, expected: Any = None
    ) -> bool: ...

    async def _retire_kiro_warm_pool(self) -> bool: ...

    def _mark_identity_epoch(self) -> None: ...

    async def _retire_kiro_subagent_runtimes(self, *, live: str = "") -> bool: ...

    async def _retire_kiro_bg_runtime(self, *, live: str = "") -> bool: ...

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
    background_key: str
    stateless_prefixes: tuple[str, ...]
    close_all_concurrency: int
    drain_active_turns_timeout_secs: float
    unbind_reason_session_destroyed: str
    first_turn_nothing_armed: object
    first_turn_fresh: object
    first_turn_resumed: object
    provider_label_claude: str
    # Singleton stateless sessions, matched exactly rather than by prefix.
    stateless_keys: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SessionLifecycleDeps:
    """Leaf dependencies supplied by the ``SessionManager`` facade.

    The facade should pass forwarding callables, rather than captured module
    globals, for every patch-sensitive dependency.  That keeps patches such as
    ``kiro_crew.session.build_provider_factory`` and
    ``kiro_crew.session.schedule_abort_for`` effective after service construction.
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
    provider_turn_is_known_terminal: Callable[[Any], bool]
    provider_uses_kiro_identity_store: Callable[[Any], bool]
    get_audit_logger: Callable[[], Any]
    #: Abort push addressed by the opaque target a provider mints, never by a pid.
    schedule_runtime_abort: Callable[..., None]
    monotonic: Callable[[], float]


@dataclass(frozen=True, slots=True)
class TornDown:
    """One teardown in flight under a key: the session its ``reset`` popped, and the process it named AT THE POP.

    ``handle`` is read off the session in the same lock hold as the pop
    (:func:`kiro_crew.process_identity.process_handle_of`) and never again: the
    teardown's own awaits can clear the provider's recorded pid while the process
    is still standing -- the ACP client's reset clears it after a kill it could not
    confirm, then hangs on the transport -- so a reader that re-read the session
    later found no pid, named no process, and recorded a run ``reaped`` over a
    live one. Immutable, so the identity the pop captured is the identity the
    reader kills on. ``session`` is kept for identity: a reader that ends the key
    counts the sessions it has answered by ``is``.
    """

    session: _SessionEntry
    handle: ProcessHandle


@dataclass(slots=True)
class SessionLifecycleState:
    """Mutable state exclusively owned by :class:`SessionLifecycleService`."""

    identity_sweep_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # The fingerprint of an identity change whose sweep has not completed. Set
    # while the sweep runs and cleared only by the sweep that completes it, so an
    # outstanding change stays its own trigger for the next turn.
    identity_sweep_fingerprint: str = ""
    # What the LAST sweep could not finish, one short label each (a session key,
    # tagged when it is a busy or channel-member holder, or the runtime that
    # stayed up). Empty after a complete sweep. Read by the caller's warning so
    # an incomplete sweep names what it is waiting on.
    identity_sweep_waiting_on: tuple[str, ...] = ()
    recycling: dict[str, _SessionEntry] = field(default_factory=dict)
    # Folded key -> the session ``reset`` popped under it, for exactly the life of
    # that teardown. ``reset`` pops the session out of the live map under the
    # registry lock BEFORE the awaits that can hang (the end record, the unlink,
    # the child probes, the provider shutdown), so from the pop to the end of the
    # teardown the map does not name the process the teardown holds. A reader
    # that must still reach it -- the cron reaper, killing a run whose OWN finally
    # reset popped the session and then hung, the ordinary shape of a run that
    # hangs in its teardown -- reads it here through
    # :meth:`SessionLifecycleService.tearing_down`. Recorded in the same lock
    # hold as the pop and released when the teardown ends however it ends
    # (return, a raised shutdown error, a cancellation landing on the hung
    # shutdown), by the :class:`_TeardownScope` the facade opens around every
    # ``reset``. One entry per teardown in flight under the key, in pop order:
    # a successor whose own reset popped it and hung beside the first teardown
    # is a process the reader must reach too -- see the scope. Each entry is a
    # :class:`TornDown`: the session AND the process handle read off it at the
    # pop, because the teardown's own awaits clear the provider's pid while the
    # process can still be standing, and a reader that re-read the session then
    # named no process at all.
    tearing_down: dict[str, list[TornDown]] = field(default_factory=dict)
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


class _TeardownScope:
    """One ``reset`` call's hold on the ``tearing_down`` entry it records at its pop.

    The facade opens a scope around every ``reset`` and hands it in; ``reset``
    records the session it pops into the scope in the same lock hold as the pop,
    and the scope's exit releases the entry. The release is the scope's and not a
    ``finally`` inside ``reset`` because the pop sits under the registry lock at
    the top of a teardown whose awaits span the rest of that method: the hold has
    to outlive every one of them, which a caller-side ``with`` does without
    re-indenting the teardown. A ``reset`` called with no scope records nothing.

    Every teardown in flight under a key holds its own entry. A key has one live
    session to pop at a time, but teardowns overlap: a cold start can register a
    successor under the key while the first teardown still hangs, and that
    successor's own reset can pop it and hang as well. A reader ending the key
    (the cron reaper) must reach BOTH processes -- retaining the first popper
    alone hid a hung successor behind a record that said ``reaped`` -- so each
    scope records the session its reset popped beside the others under the key,
    in pop order, and removes exactly that entry, by identity, when its teardown
    ends -- return, raise or cancellation alike. Bounded by construction: an
    entry lives exactly as long as the reset coroutine that recorded it, which
    already holds that session, so the table adds a reference to each teardown in
    flight and never a lifetime; a cap would only recreate the invisibility.

    The scope also answers a question the table cannot: which session did THIS
    reset pop? A caller that resets a key and then has to kill what the reset
    could not stop (the cron reaper, when its own reset times out) cannot learn
    that from a snapshot taken before the reset -- a cold start can register a
    new session under the key between the snapshot and the pop, and it is that
    session the reset pops and then hangs on. So a caller-supplied ``on_pop``
    runs in the same lock hold as the pop, for every pop, so whatever it reads
    off the session (a process handle) is read atomically with the pop and
    before any await that could lose it. The table's own entry reads the same
    handle at the same instant (:class:`TornDown`), for the readers that did
    not open this scope: the run's own finally reset, and a successor's, are
    teardowns the cron reaper never started and can reach only through the
    table -- and only on the handle captured before their awaits cleared the
    pid.
    """

    __slots__ = ("_table", "_key", "_session", "_on_pop")

    def __init__(
        self,
        table: dict[str, list[TornDown]],
        on_pop: Callable[[_SessionEntry], None] | None = None,
    ) -> None:
        self._table = table
        self._key: str | None = None
        self._session: _SessionEntry | None = None
        self._on_pop = on_pop

    def note_pop(self, key: str, session: _SessionEntry) -> None:
        """Record the session ``reset`` is popping under *key*, in the same lock hold as the pop."""
        self.retain(key, session)
        if self._on_pop is not None:
            self._on_pop(session)

    def retain(self, key: str, session: _SessionEntry) -> None:
        """Record *session*, with the process handle it names right now, among the teardowns under *key*."""
        entries = self._table.setdefault(key, [])
        if not any(entry.session is session for entry in entries):
            entries.append(TornDown(session, process_handle_of(session)))
        self._key = key
        self._session = session

    def release(self) -> None:
        """Drop the entry this scope recorded; the entries other scopes hold are left alone."""
        if self._key is not None:
            entries = self._table.get(self._key)
            if entries is not None:
                entries[:] = [entry for entry in entries if entry.session is not self._session]
                if not entries:
                    del self._table[self._key]
        self._key = None
        self._session = None

    def __enter__(self) -> "_TeardownScope":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


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

    def teardown_scope(
        self, on_pop: Callable[[_SessionEntry], None] | None = None
    ) -> _TeardownScope:
        """A hold on the ``tearing_down`` entry the ``reset`` it is handed to records.

        Opened by the facade around every ``reset`` (``with``), so the entry is
        released when the teardown ends however it ends; see :class:`_TeardownScope`.
        A caller that must know exactly which session its reset popped -- and read
        something off it atomically with the pop -- opens the scope itself, passes
        ``on_pop`` and hands the scope to ``reset``.
        """
        return _TeardownScope(self.state.tearing_down, on_pop)

    def tearing_down(self, key: str) -> list[TornDown]:
        """Every teardown in flight under *key*, in pop order: the popped session with the process handle read at its pop.

        The live map stops naming a session at the pop, before the teardown's
        awaits; a reader that must still reach those sessions' processes (the
        cron reaper, after a run's own finally reset popped the session and hung
        -- and after a successor's reset popped it and hung too) reads them here
        for exactly the life of each teardown, and kills on the entry's
        ``handle`` -- the identity captured at the pop -- never on a re-read of
        the session, whose pid the hung teardown may since have cleared. Empty
        when none is in flight. A fresh list: the table's own is not handed out.
        """
        return list(self.state.tearing_down.get(self._owner._fold_key(key), ()))

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

    # Each member below is the function of that name in the owner module its group's
    # header names (``kiro_crew.session_lifecycle_parts``), bound here by name.
    # ── Ending a parent's sub-agent runs (parent_end) ──
    set_child_teardown_handler = _parent_end.set_child_teardown_handler
    end_children_for = _parent_end.end_children_for
    _open_teardown_fence = _parent_end._open_teardown_fence
    _parent_has_sparable_child = _parent_end._parent_has_sparable_child
    _parent_owes_child_work = _parent_end._parent_owes_child_work
    _parent_owes_child_work_now = _parent_end._parent_owes_child_work_now
    _snapshot_parent_children = _parent_end._snapshot_parent_children
    _cancel_parent_children = _parent_end._cancel_parent_children
    # ── Per-key teardown verbs (teardown) ──
    reset = _teardown.reset
    _park_reset_queue = _teardown._park_reset_queue
    _release_turn_ceiling = _teardown._release_turn_ceiling
    remove = _teardown.remove
    remove_if_unclaimed = _teardown.remove_if_unclaimed
    destroy = _teardown.destroy
    destroy_if = _teardown.destroy_if
    discard_conversation = _teardown.discard_conversation
    # ── Account-identity retirement (identity_retirement) ──
    flag_identity_stamp_mismatches = _identity_retirement.flag_identity_stamp_mismatches
    retire_kiro_identity_sessions = _identity_retirement.retire_kiro_identity_sessions
    _retire_kiro_subagent_runtimes = _identity_retirement._retire_kiro_subagent_runtimes
    _retire_kiro_bg_runtime = _identity_retirement._retire_kiro_bg_runtime
    # ── A key's turn across a Stop or a reset (turn_stop) ──
    stop_generation = _turn_stop.stop_generation
    _stop_bucket = _turn_stop._stop_bucket
    note_stop = _turn_stop.note_stop
    open_replay_gap = _turn_stop.open_replay_gap
    close_replay_gap = _turn_stop.close_replay_gap
    _discard_replay_gap = _turn_stop._discard_replay_gap
    _orphan_turn_holder = _turn_stop._orphan_turn_holder
    _calling_task = staticmethod(_turn_stop._calling_task)
    absorb_orphaned_release = _turn_stop.absorb_orphaned_release
    adopt_turn = _turn_stop.adopt_turn
    _wake_turn_waiters = staticmethod(_turn_stop._wake_turn_waiters)
    await_replay_gap = _turn_stop.await_replay_gap
    cancel_current = _turn_stop.cancel_current
    cancel_owned_turn = _turn_stop.cancel_owned_turn
    stop_turn = _turn_stop.stop_turn
    _send_abort_for_session = _turn_stop._send_abort_for_session
    _eager_respawn = _turn_stop._eager_respawn
    _respawn_as = _turn_stop._respawn_as
    # ── Every session at once (fleet) ──
    refresh_defaults = _fleet.refresh_defaults
    reload_provider_factory = _fleet.reload_provider_factory
    drain_active_turns = _fleet.drain_active_turns
    close_all = _fleet.close_all
    drain_all_providers = _fleet.drain_all_providers


__all__ = [
    "CancelOutcome",
    "SessionLifecycleConstants",
    "SessionLifecycleDeps",
    "SessionLifecycleService",
    "SessionLifecycleState",
    "StopOutcome",
]


# The bodies of SessionLifecycleService's members, and the module-level stop-decline and
# parked-queue records, live in the session_lifecycle_parts owners, imported above because
# the class body binds their functions. ``_facade_mirror`` keeps every name this module
# binds one name: a write of ``kiro_crew.session_lifecycle.<name>`` reaches every owner
# binding the same object.
#: The owner modules, lowest layer first.
_OWNERS: tuple[str, ...] = (
    "kiro_crew.session_lifecycle_parts.stop_decline",
    "kiro_crew.session_lifecycle_parts.parked_queue",
    "kiro_crew.session_lifecycle_parts.parent_end",
    "kiro_crew.session_lifecycle_parts.teardown",
    "kiro_crew.session_lifecycle_parts.identity_retirement",
    "kiro_crew.session_lifecycle_parts.turn_stop",
    "kiro_crew.session_lifecycle_parts.fleet",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: The module-level names an owner rebinds at run time (``global``): served from that
#: owner rather than bound here, so a read through this module sees the live value.
_SERVED = frozenset({"_parked_dropped", "_stop_declined_dropped"})
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(
    _facade_mirror.install(
        _sys.modules[__name__],
        _OWNERS,
        excluded=(),
        surface=frozenset(globals()) | _SERVED,
    ).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.session_lifecycle_parts.parked_queue import _parked_dropped  # noqa: F401
    from kiro_crew.session_lifecycle_parts.stop_decline import _stop_declined_dropped  # noqa: F401
