"""The :class:`SubagentManager` class.

Its constructor, class attributes and properties, the durable task queue it opens and
closes, and one binding per other method, grouped under the owner that holds the method.

Nothing but ``kiro_crew.subagent`` imports a module of this package at runtime, except
``manager.py``, which imports the ``*_surface`` modules its class binds and ``run_limits``
for its default arguments.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.context_management import COMPLETION_KEEP_DEFAULT_CHARS
from kiro_crew.subagent_manager import (
    CancellationCoordinator,
    ContinuationCoordinator,
    OrphanStallMonitor,
    RunEventCoordinator,
    SpawnAdmissionCoordinator,
    TerminalCoordinator,
    WaveDigestCoordinator,
)
from kiro_crew.subagent_parts import admission_surface as _admission_surface
from kiro_crew.subagent_parts import cancellation_surface as _cancellation_surface
from kiro_crew.subagent_parts import config_surface as _config_surface
from kiro_crew.subagent_parts import continuation_surface as _continuation_surface
from kiro_crew.subagent_parts import monitor_surface as _monitor_surface
from kiro_crew.subagent_parts import run_surface as _run_surface
from kiro_crew.subagent_parts import terminal_surface as _terminal_surface
from kiro_crew.subagent_parts.run_limits import (
    _MAX_CONCURRENT,
    _STALL_IDLE_SECS,
    _TIMEOUT_SECS,
    _TURN_LIMIT,
)

if TYPE_CHECKING:
    from kiro_crew.subagent import (
        _STARTUP_COLLECT_GRACE_SECS,
        _STARTUP_HANDSHAKES,
        _STARTUP_LAUNCH_MARGIN_SECS,
        _STARTUP_TIMEOUT_SECS,
        _TEARDOWN_GATE_TTL_SECS,
        DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS,
        INITIALIZE_TIMEOUT_SECS,
        SESSION_START_TIMEOUT_MIN,
        AbstractSet,
        AgentConfig,
        AnnounceCallback,
        Any,
        Awaitable,
        Callable,
        ClaimPoint,
        ContextBuilder,
        KiroCrewConfig,
        LLMEvent,
        MemoryWait,
        ProcessHandle,
        PublishedQueueDepths,
        SessionManager,
        SpawnApprovalCallback,
        SubagentEventCallback,
        SubagentInfo,
        ToolApprovalCallback,
        _AgingIdSet,
        _PendingDepthEmit,
        _PendingDepthRetry,
        asyncio,
        data_home,
        live,
        logger,
        threading,
    )


class SubagentManager:
    """Spawn and track isolated background agents."""

    # The fields mypy reports ``[has-type]`` for when a ``*_surface`` owner or a
    # ``subagent_manager`` coordinator reads them. Both import the class only under
    # ``TYPE_CHECKING``, so mypy checks them before it infers these from ``__init__``;
    # each is declared here with its type.
    _monitor: OrphanStallMonitor
    _terminal: TerminalCoordinator
    _admission: SpawnAdmissionCoordinator
    _continuation: ContinuationCoordinator
    _waves: WaveDigestCoordinator
    _run_events: RunEventCoordinator
    _cancellation: CancellationCoordinator
    _published_depths: PublishedQueueDepths
    _startup_reservations: int
    _pressure_hold_on: bool
    _pressure_episode_spent: bool
    _pressure_episode_read_at: float
    _memory_wake_again: bool
    _spawn_stagger_secs: float
    _result_ttl_secs: int
    _subagent_queue_max_wait_secs: int
    _global_approval_mode: str

    _COMPONENT_TYPES = {
        "_monitor": OrphanStallMonitor,
        "_terminal": TerminalCoordinator,
        "_admission": SpawnAdmissionCoordinator,
        "_continuation": ContinuationCoordinator,
        "_waves": WaveDigestCoordinator,
        "_run_events": RunEventCoordinator,
        "_cancellation": CancellationCoordinator,
    }

    _reconcile_task: asyncio.Task | None  # type: ignore[type-arg]

    def __getattr__(self, name: str) -> Any:
        """Lazily compose a missing coordinator for minimal facade construction."""
        component_type = self._COMPONENT_TYPES.get(name)
        if component_type is None:
            raise AttributeError(name)
        component = component_type(self)
        object.__setattr__(self, name, component)
        return component

    def __init__(
        self,
        sessions: SessionManager,
        ctx_builder: ContextBuilder,
        on_done: AnnounceCallback | None = None,
        max_concurrent: int = _MAX_CONCURRENT,
        default_turn_limit: int = _TURN_LIMIT,
        default_timeout: int = _TIMEOUT_SECS,
        startup_timeout: int = 0,
        stall_idle_secs: int = _STALL_IDLE_SECS,
        on_tool_approval: ToolApprovalCallback | None = None,
        on_tool_approval_factory: (
            Callable[["SubagentInfo"], Callable[[LLMEvent], Awaitable[bool]]] | None
        ) = None,
        on_spawn_approval: SpawnApprovalCallback | None = None,
        is_yolo: Callable[[], bool] | None = None,
        on_event: SubagentEventCallback | None = None,
        on_orphan_notify: Callable[..., Awaitable[bool | str]] | None = None,
        on_orphan_dm: Callable[[str], Awaitable[bool]] | None = None,
        completion_keep: str = "head",
        completion_keep_chars: int = COMPLETION_KEEP_DEFAULT_CHARS,
        memory_mode_for_session: Callable[[str], str] | None = None,
        defer_queue_dispatch: bool = False,
    ):
        self._sessions = sessions
        # Run ids a parent-end teardown stopped. Keyed by ID rather than carried
        # only on the run record because a QUEUED run has no ``_agents`` row at
        # all: ``_report_queued_stop`` builds a fresh ``SubagentInfo`` for its
        # synthetic terminal, which would default the flag to False and walk
        # straight through the delivery gate. The gate reads this set, so live
        # runs, queued runs and follow-up synthetics are all covered by the one
        # place the teardown writes.
        self._teardown_cancelled_ids = _AgingIdSet(_TEARDOWN_GATE_TTL_SECS)
        # The parent-end store sweep's fence (``CancellationCoordinator``
        # ``note_teardown_snapshot``): per parent key, the ids of the rows
        # accepted for it since its latest snapshot -- a successor's, which the
        # sweep must spare. Pending here until the cancel takes it, then in
        # ``_teardown_store_sweeps`` until that cancel is done, recording all
        # along. Accept-ordered, not clock-ordered: a wall clock stepped back
        # would stamp a successor's row "before" the snapshot. Under the lock
        # because the accept that records runs on the store's writer thread.
        # A snapshot whose cancel never ran is replaced by the next one.
        self._teardown_store_fences: dict[str, set[str]] = {}
        self._teardown_store_sweeps: list[tuple[str, set[str]]] = []
        # TENTATIVE admission fences the identity sweep opens before it decides
        # retire-vs-defer: they record admissions (so a concurrent /api/spawn is
        # seen by the retire-vs-defer re-check) but do NOT drive delivery
        # suppression, because the parent may be SPARED. Only a committed teardown
        # fence (``_teardown_store_fences``) suppresses an expiring row's delivery.
        self._identity_sweep_fences: dict[str, set[str]] = {}
        # Sweeps whose store read was refused: ``(key, fence, verb)``. Each fence
        # stays in ``_teardown_store_sweeps`` (recording, and gating an expiry of
        # the retired rows) until a reaper sweep's retry has read the store.
        self._teardown_sweeps_owed: list[tuple[str, AbstractSet[str], str]] = []
        self._teardown_fence_lock = threading.Lock()
        self._memory_mode_for_session = memory_mode_for_session
        self._ctx_builder = ctx_builder
        self._on_done = on_done
        #: While True the staggered pump admits nothing: the durable rows that
        #: survived a restart wait for :meth:`release_queue_dispatch`. The
        #: gateway sets it so the boot drain (``start_reaper`` /
        #: ``_initialize_taskq``) cannot claim a row during the memory barrier;
        #: a manager built without it (tests, tools) pumps as soon as it can.
        self._queue_dispatch_held = bool(defer_queue_dispatch)
        #: Set by the pump the first time it refuses a pass under the hold, so
        #: a hold that is never released leaves one debug line behind instead
        #: of the silent "accepted, never claimed" queue this fix diagnoses.
        self._queue_dispatch_hold_logged = False
        # ``_max_concurrent`` is the EFFECTIVE cap every admission read site
        # consults: ``min(user cap, adaptive cap)``. The user's resolved cap
        # (``agent.max_subagents`` / the auto ceiling) is the ceiling in
        # ``_user_max_concurrent``; the adaptive controller lowers the runtime
        # value through :meth:`set_effective_cap` and never writes the ceiling.
        self._user_max_concurrent = max_concurrent
        self._adaptive_cap: int | None = None
        self._max_concurrent = max_concurrent
        #: Gates OUTSIDE this manager that are bounded by ``_max_concurrent``
        #: and cannot see it change (the runner lane -- TaskRunner steps and
        #: workflow ``ctx.agent()`` calls). Registered by the gateway through
        #: :meth:`set_cap_raise_listener`; None everywhere else.
        self._cap_raise_listener: Callable[[], object] | None = None
        self._default_turn_limit = default_turn_limit
        self._default_timeout = default_timeout if default_timeout > 0 else _TIMEOUT_SECS
        # A positive value pins the window; 0 derives it from the budget.
        self._startup_timeout_override = startup_timeout if startup_timeout > 0 else None
        self._stall_idle_secs = stall_idle_secs if stall_idle_secs > 0 else _STALL_IDLE_SECS
        self._on_tool_approval = on_tool_approval  # fallback for non-auto sessions
        self._on_tool_approval_factory = on_tool_approval_factory
        self._on_spawn_approval = on_spawn_approval
        self._is_yolo = is_yolo
        self._on_event = on_event
        # Orphan-notification delivery (gateway-wired). ``on_orphan_notify``
        # injects a message into the parent dashboard slot (returns True on
        # success); ``on_orphan_dm`` is the owner-DM / notification fallback.
        self._on_orphan_notify = on_orphan_notify
        self._on_orphan_dm = on_orphan_dm
        # Set by cancel_all() so shutdown-driven task cancellations never
        # trigger the one-shot unexpected-cancel auto-continue.
        self._shutting_down = False
        self._completion_keep = completion_keep
        self._completion_keep_chars = completion_keep_chars
        self._running_count = 0
        # Learned settled RSS per cost bucket (GiB), the dedicated start
        # projection's input. Replaced whole, off the loop, by the reaper's cost
        # sweep (``_refresh_learned_settled``); the gate only reads it, so it
        # never opens the cost log on the event loop.
        self._learned_settled_gb: dict[str, float] = {}
        # Set when a run records a settled reading (and at start), so the sweep
        # re-reads the cost log only when it can have changed what the map holds.
        self._learned_settled_dirty = True
        # Monotonic time of the last re-read; ``None`` until the first, so the
        # first sweep always reads.
        self._learned_settled_read_mono: float | None = None
        # Monotonic time of the oldest read whose figures the map may still
        # hold: an incomplete read keeps figures it could not confirm, but not
        # past the reading horizon.
        self._learned_settled_basis_mono: float | None = None
        # Claims admitted but not registered yet: (checked price, priced shared),
        # keyed by run id. Set with the ClaimPoint reservation, popped by the
        # re-entry that registers it or ``release_reservation``.
        self._claim_prices: dict[str, tuple[float, bool]] = {}
        # One start admitted at the shared price re-checks the floor at a time,
        # so waiters do not each count the others' raised prices and all hold.
        self._dedicated_topup_lock = asyncio.Lock()
        # Strong refs to in-flight shielded terminal reports (see
        # `_spawn_terminal_report`); drained in `cancel_all`.
        self._report_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        # follow_up watchers (spawn_steer mode="follow_up"), keyed by run id.
        # Manager-OWNED on purpose: these tasks can spawn a brand-new run
        # (continue_conversation), so per this module's containment contract
        # (see _schedule_cancel_recovery) they must be reachable by
        # cancel_all() — a watcher parked in the global _safe_fire set would
        # survive shutdown and dispatch against a closing SessionManager.
        self._followup_watchers: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        # Run id -> parent session for each live watcher. Kept separately from
        # `_agents` because completed-record eviction can remove the run while
        # its watcher still owns a follow-up dispatch.
        self._followup_watcher_parents: dict[str, str] = {}
        # Run id -> the exact record captured by each watcher. Completed-record
        # eviction may remove it from ``_agents`` while queued follow-ups still
        # own a continuation, so parent-end cancellation needs this independent index.
        self._followup_watcher_infos: dict[str, SubagentInfo] = {}
        # task -> the agent whose terminal report it is delivering
        self._report_owners: dict[asyncio.Task, SubagentInfo] = {}  # type: ignore[type-arg]
        # Whether the last queued listing was partial, so the bridge logs the
        # transition into one once rather than per request.
        self._queued_listing_partial = False
        # A post-claim store outage cannot return an ADMITTED row to the ordinary
        # refill, which reads only claimable rows. Keep that generation, its
        # reserved slot, and its re-entry callback until a later pump pass can
        # revalidate it. One entry requires one already-reserved slot, so this map
        # is bounded by the effective concurrency cap for the process lifetime.
        self._retained_claims: dict[
            str,
            tuple[
                ClaimPoint,
                int,
                Callable[[tuple[int, bool, str]], Any],
                dict[str, Any],
            ],
        ] = {}
        self._retained_claim_retry_handle: asyncio.TimerHandle | None = None
        self._last_spawn_ts: float = 0.0  # monotonic time of the last actual start (stagger gate)
        self.hook_store: Any = None  # Optional ScriptHookStore, set by server.py
        self._agents: dict[str, SubagentInfo] = {}
        # Continuable conversations: session_key ("subagent:<conv-id>") →
        # last-used ``time.monotonic()`` reading. Drives the reaper's idle-TTL
        # sweep. Rebuilt from state.json (keep=True runs) on the reaper's first
        # pass after a gateway restart, which turns each stored wall-clock stamp
        # into an age, so promoted conversations stay owned by
        # the TTL sweep across restarts; a spawn_continue on an unknown key
        # also re-registers it on demand.
        self._conversations: dict[str, float] = {}
        self._conv_registry_rebuilt = False
        # Run ids whose bounded state-write drain EXPIRED, so a pool worker is
        # still live and its stale whole-file rewrite would roll back the
        # retention `keep` a promote / release writes on the loop.
        # `_conversation_busy` reports these as held, which defers both retention
        # writes past the worker. Keyed by run id, each entry the run's workers
        # still writing; each worker's own done-callback removes itself, and the
        # id goes with the last one, so a run can hold several. It lives on the
        # MANAGER, not on the run's SubagentInfo, because `evict_completed_agents`
        # prunes completed runs out of `_agents` and an eviction must not silently
        # release the hold.
        self._abandoned_state_writers: dict[str, set[asyncio.Future[Any]]] = {}
        # Conversation ids whose continuation start is in flight: the start claim for
        # every continuation, held here and not on the opening run's registry record,
        # because ``evict_completed_agents`` can drop that record (or a restart left
        # none) while a start awaits. Taken by ``_claim_continuation`` and released
        # by ``_settle_continuation``.
        self._continuations_starting: set[str] = set()
        # state.json is the source of truth for retention: give the
        # SessionManager's in-memory continuable cache a disk fallback so a
        # cache miss (restart window) cannot demote a promoted conversation.
        try:
            self._sessions.set_continuable_fallback(self._keep_recorded_on_disk)
        except AttributeError:
            pass  # test doubles without the setter
        self._tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        # Teardown gates for runs whose terminal report has started, keyed by id and
        # OUTLIVING both dicts above. A "delivered" tombstone excludes a folder from
        # restart orphan reconciliation, so it must never be written while the run's
        # child is still being killed -- and the settlement that writes it can happen
        # outside the run (the parent's queue drain), long after a
        # dashboard "clear completed" / "cancel" has popped BOTH ``_agents`` and
        # ``_tasks`` for a done-but-still-tearing-down run. Reading the gate from
        # here, rather than inferring "record gone means teardown finished", is what
        # makes that inference unnecessary. Removed by the same ``finally`` that sets
        # the event, so a missing entry always means "nothing left to wait for".
        self._teardown_gates: dict[str, asyncio.Event] = {}
        #: run id -> the kill handles of every process its session key named,
        #: RETAINED across the reset that pops the session from the map (see
        #: :class:`kiro_crew.process_identity.ProcessHandle`).
        #: Written by :meth:`_retain_process_handles` from whichever teardown path
        #: reaches the reset first -- the run's own ``finally`` or the reaper --
        #: and read by the other on a session-map miss, so a force-stop that
        #: arrives while the first reset is hanging can still name, verify and
        #: signal the process. Cleared when the path that holds it has decided
        #: (the handles were consumed by the kill, or the survivor check found
        #: the processes gone); an entry that outlives its run is one small record.
        self._process_handles: dict[str, list[ProcessHandle]] = {}
        # Queued spawns store the FULL spawn() kwarg set (not just a 5-tuple), so a
        # drained spawn preserves approval_mode / silent / model / allowed_tools / bare —
        # dropping them made a queued headless/auto spawn hit the deny-by-default gate and
        # a queued silent spawn emit output. See _drain_queue.
        self._queue: list[dict[str, Any]] = []
        # parent_session_key -> the last wait the gate labelled for that parent
        # (``{"reason": <QUEUED_REASON_*>, "available_gb"?, "required_gb"?}``).
        # ``_emit_queue_depth`` attaches it to every ``subagent_queued`` it sends
        # while the parent still has rows waiting, and forgets it at depth 0: the
        # drain and the cancel paths re-emit the depth without a verdict of their
        # own, and without this memory each re-emit would flip a memory-deferred
        # wave back to the default (concurrency) text.
        self._queue_wait: dict[str, dict[str, Any]] = {}
        # parent_session_key -> its one in-flight coalesced depth emit. An entry
        # lives only while that emit's read task does (``_emit_queue_depth``;
        # the task's own completion drops it), so it is bounded by the parents
        # with a read in flight and needs no eviction when a parent ends.
        self._queue_depth_emits: dict[str, _PendingDepthEmit] = {}
        # parent_session_key -> its one armed delayed re-read after a depth read
        # the store could not answer; cancelled by the next frame for that
        # parent and at shutdown, so at most one per parent.
        self._queue_depth_retries: dict[str, _PendingDepthRetry] = {}
        # Rows a ``spawn_async`` caller is still admitting that the gate has
        # already queued (deferred or behind the cap): the refill still leaves
        # them to their caller, but the queue-depth chip counts them. Marked
        # and cleared by that call alone (``_spawn_async_accepted``,
        # ``spawn_async``'s ``finally``), so always a subset of
        # ``_admitting_ids``.
        self._admitting_waiting: set[str] = set()
        # When the refill last reported a deferred row stuck past its wake
        # (monotonic); the report is rate-limited (``_refill_schedule_wake``).
        self._overdue_wake_warned_at: float = float("-inf")
        # The macOS kernel memory-pressure hold (``_memory_pressure_hold``): the
        # level it last warned at (None between episodes, so each episode warns
        # again), whether it applied at the last read, each held root start's
        # first-held time, the rows whose wait ran out (released, said once),
        # and the one re-check timer while it applies.
        self._pressure_hold_level: int | None = None
        self._pressure_hold_on = False
        # When the current kernel episode (level WARN or worse) was first read,
        # and whether it has outlived the bound, which ends the starts it would
        # hold, never started, until the level eases.
        self._pressure_episode_since: float | None = None
        self._pressure_episode_spent = False
        self._pressure_episode_read_at = 0.0
        self._pressure_holds: dict[str, float] = {}
        self._pressure_hold_expired: set[str] = set()
        # The pressure hold's per-lane progress. A lane is the parent session a
        # root row waits behind (``taskq.lanes.lane_key_for``), the one the
        # pump's weighted pick serves FIFO. A held row's bound counts from the
        # later of its first hold and its lane's last root start, so a lane
        # that keeps starting rows never expires the rows waiting behind them,
        # whichever wave they came from, and a lane none of whose rows starts
        # still ends them after the bound. Another lane's starts never extend
        # it; automation keys all share ``SYSTEM_LANE``, one FIFO queue, so
        # they are one lane here too. agent_id -> its lane, lane -> how many
        # rows it holds, and
        # lane -> its last root start (monotonic), kept only while the lane
        # holds a row; ``_forget_pressure_clock`` drops all three.
        self._pressure_hold_lanes: dict[str, str] = {}
        self._pressure_lane_held: dict[str, int] = {}
        self._pressure_lane_started_at: dict[str, float] = {}
        # agent_id -> (closed parked time, current park's start, its planned
        # end), all integer ``time.monotonic_ns()`` nanoseconds, for a start
        # with no durable row that waits on the memory floor: the store sweep
        # cannot see it, so the gate bounds it from this clock
        # (``agent.subagent_queue_max_wait_secs``) at each re-park.
        self._floor_waits: dict[str, tuple[int, int, int]] = {}
        self._pressure_recheck_handle: asyncio.TimerHandle | None = None
        # agent_id -> the ``approval_mode`` a durable row was accepted with, for
        # as long as it waits: the store never carries it, so a window refill
        # restores it from here (``_refill_apply``).
        self._held_approval_modes: dict[str, str] = {}
        # Durable rows whose last defer was the memory floor's (``low_memory``):
        # the store keeps only ``next_run_at``, so a window refill stamps the
        # entry as a floor wait from here (``_refill_apply``) and the pump's pick
        # leaves it to the gate, which re-checks the floor before the pressure
        # hold. Process-local, as the hold's own clocks are.
        self._floor_deferred_ids: set[str] = set()
        # The starts this process holds back for memory, from their first
        # deferral to their admission, start or end (subagent.md, *Memory
        # waits: event wake and the per-lane share*): what the event wake
        # brings forward, what the per-lane share reads, and what keeps the
        # sampler running -- it runs only while this is not empty.
        self._memory_waits: dict[str, "MemoryWait"] = {}
        # The fit pass in flight (``wake_memory_waits``), and whether a wake
        # arrived during it, so one more pass runs after it.
        self._memory_fit_task: "asyncio.Task[None] | None" = None
        self._memory_wake_again = False
        self._memory_sampler_handle: asyncio.TimerHandle | None = None
        self._memory_sample_task: "asyncio.Task[None] | None" = None
        # Rows the pump has popped from the window but not yet claimed. Their
        # durable state is still QUEUED, so without this set every store-backed
        # depth read between pop and claim counts them as waiting.
        self._dispatching_ids: set[str] = set()
        # The same popped ids, read by ``is_queued`` (the serial-lock
        # done-probe) while a row is in neither ``_queue`` nor ``_agents``:
        # without it the probe reads such a row as finished and releases the
        # guard. It is the ONLY record of a popped non-durable row; a durable
        # one is also in the store's unstarted index. Marked with
        # ``_dispatching_ids`` but outlives it across a retained claim, which
        # still holds pending work until the retry registers or refuses it
        # (``retry_retained_claims``).
        self._dispatch_window_ids: set[str] = set()
        # A popped row with no durable record (incognito or temporary memory,
        # or the task queue off), keyed by id, while the coroutine pump awaits
        # its off-loop reads before the gate starts it. It is the row's ONLY
        # copy then: a stop finds it here (``_unqueue``), the pump re-checks it
        # after each await and does not start a row a stop took, and a dispatch
        # that raises puts it back in the window instead of losing it. A durable
        # row needs none of this: the store keeps it and the claim re-checks
        # the stop.
        self._undurable_in_dispatch: dict[str, dict[str, Any]] = {}
        # parent_session_key -> how many Stop all calls for it are in flight.
        # While one is, the refill windows none of that parent's rows: a fetch
        # queued before the stop would otherwise put back rows the stop is
        # cancelling, or window store-only rows its pending read then skips.
        self._stopping_parents: dict[str, int] = {}
        # agent_id -> the per-row answer of the Stop all batch that is
        # cancelling its row and has not reported it yet. A single ``cancel``
        # of such a row joins the batch instead of cancelling and reporting it
        # again (``cancel_impl``), and a claim whose re-read the cancel may
        # have overtaken waits for it and re-reads (``claim_and_start``): the
        # row is in neither ``_queue`` nor ``_agents``.
        self._batched_stops: dict[str, asyncio.Future[Any]] = {}
        # agent_id -> the parent of each row filed in ``_batched_stops``, for
        # as long as it is filed: a parent-end teardown's snapshot gates the
        # batch's report of a row that is in neither ``_queue`` nor ``_agents``.
        self._batched_stop_parents: dict[str, str] = {}
        # Batch ids whose spawn_batch_started event has already fired.
        self._seen_batches: set[str] = set()
        # Submission accounting per wave: batch_id -> (submitted, expected).
        # Guards the wave digest against firing before every member's POST has
        # arrived — a fast-failing first member must not let the completion
        # fallback see "no pending members" while later submissions are still
        # in flight. Pruned by finalize_batch().
        self._batch_submitted: dict[str, list[int]] = {}
        # Wave liveness: last submission-progress time.monotonic() per batch_id,
        # aged against the reaper's monotonic reading so a wall-clock step cannot
        # move it.
        # Drives the reaper's stuck-wave backstop (a wave with lost
        # submissions and no progress is force-reconciled). Pruned by
        # finalize_batch alongside _batch_submitted.
        self._batch_progress_ts: dict[str, float] = {}
        # parent_session_key -> the ``subagent_queued`` depth last published for
        # it, recorded by ``_fire_event`` and serialized on each slot as
        # ``subagents_queued`` (see ``subagent_manager.published_depth``).
        self._published_depths = PublishedQueueDepths()
        # Done-but-unreported holds per wave: batch_id -> agent ids whose
        # ``done`` has flipped but whose terminal report the completion
        # consumer has not yet consumed. Held OUTSIDE the agent records
        # because ``_agents`` membership is operator-mutable (DELETE
        # /api/spawn pops done members) -- the wave-close fallback must not
        # lose its hold to a concurrent clear. Armed by the
        # report machinery at the done-flip, disarmed by the consumer when
        # the contribution lands or structurally when a report ends without
        # reaching the consumer. Pruned by finalize_batch.
        self._reports_in_flight: dict[str, set[str]] = {}
        self._reaper_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # Every ``_force_reap`` that runs OUTSIDE the reaper task -- a dashboard
        # Stop or a parent-end cancel, each awaited inside its
        # caller's own task. ``cancel_all`` cancels these beside the reaper task
        # so their cancellation arms finish the record and release the report
        # inside the shutdown drain: a reap the shutdown never touched sat in
        # its hanging reset until the gateway's budget hard-exited the process,
        # with its report waiting on a gate nobody released.
        self._reap_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        # The one reap in flight per run, by agent id: a future its owner settles
        # on every exit. A second stop path arriving while it is pending (a
        # dashboard Stop racing a deadline reap, or the reverse) joins it
        # instead of running a second reap, so the kill is decided, recorded
        # and reported once. See ``_force_reap``.
        self._reaps_in_flight: dict[str, asyncio.Future] = {}  # type: ignore[type-arg]
        # Cache global approval_mode at init to avoid disk I/O on every
        # parentless spawn (cron, webhooks).
        try:
            self._global_approval_mode = KiroCrewConfig.load().agent.approval_mode
        except Exception:
            logger.warning(
                "Failed to load KiroCrewConfig for approval_mode; defaulting to interactive",
                exc_info=True,
            )
            self._global_approval_mode = ""
        # Retention window (seconds) for a delivered subagent's result.txt before
        # the reaper prunes it — the parent's grace window to read the full
        # transcript (spawn_status / read / grep) after the completion event.
        try:
            self._result_ttl_secs = int(KiroCrewConfig.load().agent.subagent_result_ttl_secs)
        except Exception:
            self._result_ttl_secs = 3600
        # Spawn stagger interval — serializes cold starts so a high cap fills as
        # a ramp rather than a burst (dynamic-subagent-sizing.md §5.3). It is a
        # smoothing interval, not the memory guard: every spawn still clears
        # ``spawn_min_memory_gb`` and the host budget, and the adaptive
        # controller cuts the cap on real pressure.
        try:
            self._spawn_stagger_secs = max(
                0.0, float(KiroCrewConfig.load().agent.subagent_spawn_stagger_secs)
            )
        except Exception:
            self._spawn_stagger_secs = 0.25
        # In-startup bound (dynamic-subagent-sizing.md, Configuration). The
        # stagger bounds the RATE of starts and ``_max_concurrent`` the RUNNING
        # population; :meth:`_startup_cap` bounds how many admitted agents may
        # be between execution start and their first runtime/stream/turn at
        # once, derived from the session-start gate's width alone -- there is
        # no key for it (see ``_startup_cap`` for why). ``session_start_concurrency``
        # is boot-only (``restart=True``), so it is captured here and never
        # re-read by ``apply_limits``: the SessionStartGate it sizes is fixed
        # for the loop's lifetime, and the derived bound must track the gate
        # that exists, not a value nothing is serving yet.
        # The manager is built on the gateway boot path and ``_startup_cap`` runs
        # on the event loop, so neither may probe the host for ``"auto"``. The
        # configured value is captured here (the same config read this block
        # always made); ``_startup_cap`` sizes it from the cached host reading
        # the gateway takes in a worker thread after the socket binds, and uses
        # the floor until that reading exists. ``_session_start_concurrency``
        # stays None unless a test pins a width.
        try:
            self._session_start_configured: object = (
                KiroCrewConfig.load().agent.session_start_concurrency
            )
        except Exception:
            self._session_start_configured = 2
        self._session_start_concurrency: int | None = None
        # ClaimPoint reservations not yet registered as a SubagentInfo; counted
        # by ``_startup_population`` (see there). +1 at reserve, -1 on the
        # re-entry that registers or on ``release_reservation``.
        self._startup_reservations = 0

        # Every limit captured above is a copy of config.json. The live watcher
        # pushes a rewrite at this object through ``reconfigure`` so a write from
        # the dashboard, ``kirocrew config set`` or ``$EDITOR`` lands without a
        # gateway restart. The subscription holds ``self`` weakly, so a discarded
        # manager (tests, provider reloads) falls out of the registry by itself.
        # The prefixes ARE the watched-path list, so the dispatcher filters an
        # unrelated ``agent.*`` write rather than the applier re-deriving on it.
        # ``None`` until the first ``reconfigure`` runs, so that first reload
        # always resolves the cap rather than comparing against a snapshot that
        # was never taken.
        self._last_sizing_fields: tuple[object, ...] | None = None
        self._config_sub: live.Subscription | None = None
        try:
            self._config_sub = live.watch_object(
                self, *self.LIVE_CONFIG_PATHS, name="SubagentManager"
            )
        except Exception:
            logger.warning("SubagentManager could not subscribe to live config", exc_info=True)

        # The facade is the single owner of mutable registries and slot tokens.
        # Coordinators own transition logic and route every cross-boundary call
        # back through this object, preserving overrides and monkeypatch seams.
        self._monitor = OrphanStallMonitor(self)
        self._terminal = TerminalCoordinator(self)
        self._admission = SpawnAdmissionCoordinator(self)
        # Durable task queue (``kiro_crew.taskq``): ``_queue`` above is a bounded
        # window over this store's rows. Schema, import and reconcile must
        # finish before attachment. Loop callers open in a worker; synchronous
        # callers open inline. Pending or failed opens refuse typed; only
        # agent.task_queue_enabled=false selects the in-memory queue.
        # ``admitted -> starting`` is written at claim; ``running`` at the
        # run's first stream event addressed to its session; waits and wakes
        # through admission.
        self._taskq: Any = None
        self._taskq_admit_wait_secs: float = 30.0
        #: ``agent.subagent_queue_max_wait_secs``: how long a memory-deferred row
        #: may wait before the pump ends it (``taskq_expire_memory_waits``); 0 is
        #: no bound. Live: :meth:`apply_limits` adopts a rewrite.
        try:
            self._subagent_queue_max_wait_secs = max(
                0, int(KiroCrewConfig.load().agent.subagent_queue_max_wait_secs)
            )
        except Exception:
            self._subagent_queue_max_wait_secs = DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS
        #: Set when ``agent.task_queue_enabled`` is on but the store could not
        #: be opened: every spawn is then REFUSED (typed, ``task_store_unavailable``)
        #: instead of accepted into an in-memory queue that a restart forgets.
        self._taskq_unavailable: str | None = None
        #: The re-open schedule for that refusal (``taskq_reopen_if_due``, driven
        #: by the reaper sweep). The count is how many opens have failed, which is
        #: the exponent of the shared recovery backoff; the deadline is monotonic,
        #: and 0.0 means the next sweep may attempt one.
        self._taskq_reopen_attempts: int = 0
        self._taskq_reopen_at: float = 0.0
        self._continuation = ContinuationCoordinator(self)
        self._waves = WaveDigestCoordinator(self)
        self._run_events = RunEventCoordinator(self)
        self._cancellation = CancellationCoordinator(self)
        self._taskq_init_task: asyncio.Task[None] | None = None
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or not SpawnAdmissionCoordinator.open_store_off_loop:
            self._taskq = self._open_taskq()
        else:
            self._taskq_unavailable = "durable task queue is initializing"
            self._taskq_init_task = loop.create_task(self._initialize_taskq())
            self._admission.track_store_task(self._taskq_init_task)

    def _open_taskq(self) -> Any:
        """Open, migrate and reconcile on a worker, or in a synchronous caller."""
        try:
            cfg = KiroCrewConfig.load()
            self._taskq_admit_wait_secs = float(cfg.agent.admit_wait_secs)
            if not cfg.agent.task_queue_enabled:
                self._taskq_unavailable = None
                return None
            store = self._admission.taskq_open(cfg, home=data_home())
            if store is None and self._taskq_unavailable is None:
                self._taskq_unavailable = "durable task queue could not be opened"
                self._admission.taskq_arm_reopen(cfg)
            return store
        except Exception as exc:
            logger.warning("durable task queue could not be opened", exc_info=True)
            self._taskq_unavailable = f"durable task queue could not be opened: {exc}"
            # Whoever records the refusal arms its retry, so no refusal path can
            # leave the store waiting for a restart. No cfg here: the load itself
            # is one of the things that may have raised.
            self._admission.taskq_arm_reopen()
            return None

    async def _initialize_taskq(self) -> None:
        opening = asyncio.create_task(asyncio.to_thread(self._open_taskq))
        try:
            store = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # The thread still owns the open; retrieve and close its result.
            store = await opening
            if store is not None:
                await asyncio.to_thread(store.close)
            raise
        if getattr(self, "_shutting_down", False):
            if store is not None:
                await asyncio.to_thread(store.close)
            return
        if store is None and self._taskq is not None:
            # A re-open never UN-attaches: this task can be one the reaper armed
            # while the refusal stood, and an attach that happened meanwhile is
            # the newer fact.
            return
        # Attach only after schema, integrity check, imports and reconcile finish.
        # Before this assignment every entry point returns task_store_unavailable.
        self._taskq = store
        if store is not None:
            self._taskq_unavailable = None
            self._taskq_reopen_attempts = 0
            if self._reaper_task is not None and not self._reaper_task.done():
                self._admission.taskq_schedule_owed_replay()
                self._drain_queue()

    async def wait_taskq_ready(self) -> None:
        """Wait for startup recovery without cancelling it if this caller leaves."""
        if self._taskq_init_task is not None:
            await asyncio.shield(self._taskq_init_task)

    def close(self) -> None:
        """Release the process-lifetime durable task store this manager opened.

        ``__init__`` opens the durable task queue (a SQLite connection plus its
        dedicated writer thread, see :class:`~kiro_crew.taskq.store.TaskStore`);
        ``cancel_all`` cancels in-flight runs but never touches that store, so
        without this every manager leaks the connection's descriptors and its
        writer executor for the life of the process. Idempotent and safe to call
        from a synchronous teardown: it cancels a still-pending async open, then
        closes the store if one was attached.
        """
        taskq_open_task = self._taskq_init_task
        if taskq_open_task is not None and not taskq_open_task.done():
            taskq_open_task.cancel()
        self._taskq_init_task = None
        store, self._taskq = self._taskq, None
        if store is not None:
            store.close()

    #: Dotted config paths whose value this manager copies at construction. They
    #: are the subscription's prefixes, so a reload that touches none of them
    #: never reaches this object; one that touches any of them re-derives EVERY
    #: copy from the new config (cheaper and safer than a per-field diff, and the
    #: derivations are all O(1)).
    LIVE_CONFIG_PATHS: tuple[str, ...] = (
        "agent.max_subagents",
        "agent.subagent_auto_max",
        "agent.spawn_min_memory_gb",
        "agent.subagent_mem_buffer_pct",
        "agent.subagent_cost_gb",
        "session.pool_size",
        "agent.subagent_max_turns",
        "agent.subagent_timeout_secs",
        "agent.subagent_stall_idle_secs",
        "agent.subagent_spawn_stagger_secs",
        "agent.subagent_result_ttl_secs",
        "agent.completion_keep",
        "agent.completion_keep_chars",
        "agent.subagent_queue_max_wait_secs",
    )

    #: The subset of ``LIVE_CONFIG_PATHS`` that actually feeds
    #: :func:`resolve_max_subagents` (the explicit pin, the auto ceiling, and
    #: the floor switch plus the memory terms the auto cap is sized from when
    #: ``agent.spawn_min_memory_gb`` disables the floor). Every other watched
    #: path only affects a plain field copy in :meth:`apply_limits`, so a
    #: reload that touches none of these has no way to change the resolved cap
    #: and must not pay for :func:`resolve_max_subagents`'s memory probe.
    SIZING_CONFIG_PATHS: tuple[str, ...] = (
        "agent.max_subagents",
        "agent.subagent_auto_max",
        "agent.spawn_min_memory_gb",
        "agent.subagent_mem_buffer_pct",
        "agent.subagent_cost_gb",
        "session.pool_size",
    )

    @property
    def _startup_deadline(self) -> int:
        """Seconds a new start may run with no runtime, from the live config."""
        if self._startup_timeout_override is not None:
            return self._startup_timeout_override
        snap = live.snapshot()
        agent = snap.agent if snap is not None else AgentConfig()
        budget = max(SESSION_START_TIMEOUT_MIN, agent.session_start_timeout_secs)
        collect = agent.start_collect_timeout_secs + _STARTUP_COLLECT_GRACE_SECS
        # ``_INITIALIZE_TIMEOUT`` as well, once per handshake round: the clock
        # pauses in a queue but runs through the spawn's own handshakes, so a
        # retried start that spends its whole ``initialize`` and ``session/new``
        # budgets twice must still be inside the window -- or the watchdog reaps
        # a start whose own budgets have not expired.
        handshakes = _STARTUP_HANDSHAKES * (budget + INITIALIZE_TIMEOUT_SECS)
        derived = int(handshakes + collect + _STARTUP_LAUNCH_MARGIN_SECS)
        return max(_STARTUP_TIMEOUT_SECS, derived)

    @property
    def user_max_concurrent(self) -> int:
        """The user's resolved cap -- the ceiling the adaptive cap sits under."""
        return self._user_max_concurrent

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def running_count(self) -> int:
        return self._running_count

    @property
    def pending_work_count(self) -> int:
        """Return accepted subagent work that a process restart would interrupt.

        ``running_count`` alone stops representing work before shielded terminal
        delivery finishes, and an unexpected-cancel recovery can be live while
        holding no concurrency slot. Count the finite manager-owned registries
        instead, while retaining ``running_count`` as a fail-closed floor for a
        slot published just before its task registration. The perpetual reaper
        is maintenance and is deliberately excluded; its one-shot orphan
        reconciliation is finite delivery work and is included.
        """
        live_primary: set[int] = set()
        for task in self._tasks.values():
            if not task.done():
                live_primary.add(id(task))

        pending = len(self._queue) + max(max(0, int(self._running_count)), len(live_primary))
        seen = set(live_primary)
        extra_tasks = [*self._report_tasks, *self._followup_watchers.values()]
        reconcile = getattr(self, "_reconcile_task", None)
        if reconcile is not None:
            extra_tasks.append(reconcile)
        for task in extra_tasks:
            marker = id(task)
            if marker in seen:
                continue
            seen.add(marker)
            if not task.done():
                pending += 1

        # A cancelled to_thread state write can outlive its run task. Each entry
        # is a run with at least one such worker still writing, and the last of
        # them to land removes it from its done-callback, so every entry is
        # finite restart-sensitive work even though no awaitable is retained here.
        pending += len(self._abandoned_state_writers)
        return pending

    # Bounds for the follow_up watcher: poll cadence, post-done busy retries
    # (finalization may briefly hold the conversation), and a hard deadline so
    # a wedged run can never leave an immortal watcher behind.
    _FOLLOWUP_POLL_SECS = 2.0

    _FOLLOWUP_BUSY_RETRIES = 10

    _FOLLOWUP_BUSY_RETRY_SECS = 3.0

    @property
    def running(self) -> list[SubagentInfo]:
        """Return currently running (not done) subagents."""
        return [a for a in self._agents.values() if not a.done]

    @property
    def all_agents(self) -> list[SubagentInfo]:
        """Return all tracked subagents (running and done)."""
        return list(self._agents.values())

    @property
    def count(self) -> int:
        return len(self.running)

    @property
    def queued_count(self) -> int:
        """Return all not-yet-registered spawns in the stagger queue."""
        return len(self._queue)

    # subagent_parts/config_surface.py
    _effective_turn_limit = _config_surface._effective_turn_limit
    update_completion_keep = _config_surface.update_completion_keep
    _sizing_fields = staticmethod(_config_surface._sizing_fields)
    reconfigure = _config_surface.reconfigure
    apply_limits = _config_surface.apply_limits
    release_queue_dispatch = _config_surface.release_queue_dispatch
    _clamp_effective_cap = _config_surface._clamp_effective_cap
    set_cap_raise_listener = _config_surface.set_cap_raise_listener
    _notify_cap_raised = _config_surface._notify_cap_raised
    set_effective_cap = _config_surface.set_effective_cap

    # subagent_parts/admission_surface.py
    _mint_agent_id = _admission_surface._mint_agent_id
    _in_startup = staticmethod(_admission_surface._in_startup)
    _startup_population = _admission_surface._startup_population
    _startup_cap = _admission_surface._startup_cap
    _resolved_session_start_width = _admission_surface._resolved_session_start_width
    _note_startup_progress = _admission_surface._note_startup_progress
    _leave_startup = _admission_surface._leave_startup
    running_agents_for = _admission_surface.running_agents_for
    task_memory_rows = _admission_surface.task_memory_rows
    spawn = _admission_surface.spawn
    _spawn_after_memory_read = _admission_surface._spawn_after_memory_read
    prepare_spawn = _admission_surface.prepare_spawn
    _check_agent_off_loop = _admission_surface._check_agent_off_loop
    spawn_async = _admission_surface.spawn_async
    _spawn_async_accepted = _admission_surface._spawn_async_accepted
    _safe_announce = _admission_surface._safe_announce
    _announce_rejection = _admission_surface._announce_rejection
    _should_stagger_queue = _admission_surface._should_stagger_queue
    _memory_pressure_hold = _admission_surface._memory_pressure_hold
    memory_pressure_hold_active = _admission_surface.memory_pressure_hold_active
    _memory_pressure_holds = _admission_surface._memory_pressure_holds
    _pressure_lane_started = _admission_surface._pressure_lane_started
    _forget_pressure_clock = _admission_surface._forget_pressure_clock
    _forget_pending_start = _admission_surface._forget_pending_start
    _drain_queue = _admission_surface._drain_queue
    _admit_released_start = _admission_surface._admit_released_start
    _release_admitted_start = _admission_surface._release_admitted_start
    _drain_queue_async = _admission_surface._drain_queue_async
    _drain_queue_pass = _admission_surface._drain_queue_pass
    _drain_queue_sync = _admission_surface._drain_queue_sync
    _dispatch_async = _admission_surface._dispatch_async
    _after_dispatch = _admission_surface._after_dispatch
    _spawn_with_approval = _admission_surface._spawn_with_approval
    _log_spawned = _admission_surface._log_spawned
    queued_run_async = _admission_surface.queued_run_async
    queued_runs_async = _admission_surface.queued_runs_async

    # subagent_parts/continuation_surface.py
    _conversation_busy = _continuation_surface._conversation_busy
    _keep_recorded_on_disk = _continuation_surface._keep_recorded_on_disk
    _promote_conversation = _continuation_surface._promote_conversation
    _scan_keep_states = _continuation_surface._scan_keep_states
    _rebuild_conversation_registry = _continuation_surface._rebuild_conversation_registry
    native_child_resume_refusal = _continuation_surface.native_child_resume_refusal
    claim_retry = _continuation_surface.claim_retry
    settle_retry = _continuation_surface.settle_retry
    _claim_continuation = _continuation_surface._claim_continuation
    _settle_continuation = _continuation_surface._settle_continuation
    continue_conversation = _continuation_surface.continue_conversation
    continue_conversation_async = _continuation_surface.continue_conversation_async
    _continue_prelude = _continuation_surface._continue_prelude
    recorded_cwd = _continuation_surface.recorded_cwd
    _inherited_context_groups = _continuation_surface._inherited_context_groups
    _inherited_memory_store = _continuation_surface._inherited_memory_store
    steer_run = _continuation_surface.steer_run
    follow_up_run = _continuation_surface.follow_up_run
    _arm_followup_watcher = _continuation_surface._arm_followup_watcher
    _deliver_followups = _continuation_surface._deliver_followups
    _announce_followup_failure = _continuation_surface._announce_followup_failure
    _audit_followup = _continuation_surface._audit_followup
    release_conversation = _continuation_surface.release_conversation
    release_conversation_async = _continuation_surface.release_conversation_async
    _sweep_conversations_async = _continuation_surface._sweep_conversations_async

    # subagent_parts/monitor_surface.py
    start_reaper = _monitor_surface.start_reaper
    _reconcile_orphans = _monitor_surface._reconcile_orphans
    _end_leaked_processes = _monitor_surface._end_leaked_processes
    _end_orphan_process = _monitor_surface._end_orphan_process
    _is_pid_alive = staticmethod(_monitor_surface._is_pid_alive)
    _is_orphan_process = staticmethod(_monitor_surface._is_orphan_process)
    _kill_orphan_pid = staticmethod(_monitor_surface._kill_orphan_pid)
    _notify_orphan = _monitor_surface._notify_orphan
    _try_inject_orphan_notification = _monitor_surface._try_inject_orphan_notification
    _send_orphan_slack_dm = _monitor_surface._send_orphan_slack_dm
    _live_shared_count = _monitor_surface._live_shared_count
    _sample_live_costs = _monitor_surface._sample_live_costs
    _refresh_learned_settled = _monitor_surface._refresh_learned_settled
    _record_cost = _monitor_surface._record_cost
    _reaper_loop = _monitor_surface._reaper_loop
    _is_startup_stalled = _monitor_surface._is_startup_stalled
    _note_tool_dispatch = staticmethod(_monitor_surface._note_tool_dispatch)
    _note_tool_result = staticmethod(_monitor_surface._note_tool_result)
    _clear_tool_dispatch = staticmethod(_monitor_surface._clear_tool_dispatch)
    _retire_subagent_oracle = staticmethod(_monitor_surface._retire_subagent_oracle)
    _stall_verdict = _monitor_surface._stall_verdict
    _maybe_flag_stall = _monitor_surface._maybe_flag_stall
    _record_slow_command = staticmethod(_monitor_surface._record_slow_command)
    _dependency_coordinator = _monitor_surface._dependency_coordinator
    dependency_coordinator = _monitor_surface.dependency_coordinator
    dependency_coordinator_async = _monitor_surface.dependency_coordinator_async
    _taskq_pump = _monitor_surface._taskq_pump

    # subagent_parts/terminal_surface.py
    _claim_finalize = _terminal_surface._claim_finalize
    _report_terminal = _terminal_surface._report_terminal
    _report_terminal_guarded = _terminal_surface._report_terminal_guarded
    _run_terminal_report = _terminal_surface._run_terminal_report
    _spawn_terminal_report = _terminal_surface._spawn_terminal_report
    _await_report = staticmethod(_terminal_surface._await_report)
    settle_before_delete = _terminal_surface.settle_before_delete
    _release_slot = _terminal_surface._release_slot
    _force_reap = _terminal_surface._force_reap
    _reap_once = _terminal_surface._reap_once
    _sigkill_session = _terminal_surface._sigkill_session
    _sigkill_sessions = _terminal_surface._sigkill_sessions
    _sessions_under = _terminal_surface._sessions_under
    _retain_process_handles = _terminal_surface._retain_process_handles
    notify_injection_failed = _terminal_surface.notify_injection_failed
    batch_members_pending = _terminal_surface.batch_members_pending
    batch_reports_in_flight = _terminal_surface.batch_reports_in_flight
    arm_report_in_flight = _terminal_surface.arm_report_in_flight
    consume_report_hold = _terminal_surface.consume_report_hold
    batch_members_pending_async = _terminal_surface.batch_members_pending_async
    wave_has_live_nested_spawns = _terminal_surface.wave_has_live_nested_spawns
    finalize_batch = _terminal_surface.finalize_batch
    record_lost_submission = _terminal_surface.record_lost_submission
    _sweep_stuck_waves = _terminal_surface._sweep_stuck_waves
    _sweep_stuck_waves_async = _terminal_surface._sweep_stuck_waves_async
    _sweep_digest_holds = _terminal_surface._sweep_digest_holds
    _sweep_digest_holds_async = _terminal_surface._sweep_digest_holds_async
    force_digest_flush = _terminal_surface.force_digest_flush
    _announce_digest_flush = _terminal_surface._announce_digest_flush
    settle_queued_delivery = _terminal_surface.settle_queued_delivery
    _settle_digest_holds = _terminal_surface._settle_digest_holds
    _write_tombstone = staticmethod(_terminal_surface._write_tombstone)

    # subagent_parts/run_surface.py
    has_live_shared_session = _run_surface.has_live_shared_session
    get = _run_surface.get
    is_queued = _run_surface.is_queued
    _teardown_run_session = _run_surface._teardown_run_session
    _run = _run_surface._run
    _touch_activity = _run_surface._touch_activity
    _fire_event = _run_surface._fire_event
    _queued_depth = _run_surface._queued_depth
    _queued_depth_async = _run_surface._queued_depth_async
    queued_count_for = _run_surface.queued_count_for
    queued_count_for_async = _run_surface.queued_count_for_async
    published_queued_depths = _run_surface.published_queued_depths
    has_in_memory_pending_work_for = _run_surface.has_in_memory_pending_work_for
    has_unfinished_report_for = _run_surface.has_unfinished_report_for
    queued_count_or_none_async = _run_surface.queued_count_or_none_async
    has_pending_work_for = _run_surface.has_pending_work_for
    has_pending_work_for_async = _run_surface.has_pending_work_for_async
    _emit_queue_depth = _run_surface._emit_queue_depth
    _write_state_off_loop = _run_surface._write_state_off_loop
    _write_finished_result_off_loop = _run_surface._write_finished_result_off_loop
    _start_result_file = _run_surface._start_result_file
    _drain_state_writer = _run_surface._drain_state_writer
    _hold_for_detached_writer = _run_surface._hold_for_detached_writer
    _cap_unclaimed_result = _run_surface._cap_unclaimed_result
    _record_reap_ending = _run_surface._record_reap_ending
    _run_inner = _run_surface._run_inner
    _stop_recovery_wanted = _run_surface._stop_recovery_wanted
    _yield_for_stop_recovery = _run_surface._yield_for_stop_recovery
    _await_lane_resume = _run_surface._await_lane_resume
    _yield_for_dependency = _run_surface._yield_for_dependency
    _yield_for_infra_retry = _run_surface._yield_for_infra_retry
    _stop_error_text = _run_surface._stop_error_text
    _taskq_note_stop_recovery = _run_surface._taskq_note_stop_recovery
    _should_use_session_sharing = _run_surface._should_use_session_sharing
    _sharing_plan = _run_surface._sharing_plan
    _ensure_dedicated_start_priced = _run_surface._ensure_dedicated_start_priced
    _create_shared_session = _run_surface._create_shared_session
    _gate_exit_reset = _run_surface._gate_exit_reset
    _gate_wait_mark = _run_surface._gate_wait_mark
    _await_late_start = _run_surface._await_late_start
    _bind_shared_handle = _run_surface._bind_shared_handle
    _get_parent_runtime = _run_surface._get_parent_runtime
    _is_cc_provider = staticmethod(_run_surface._is_cc_provider)
    _provider_label_of = staticmethod(_run_surface._provider_label_of)

    # subagent_parts/cancellation_surface.py
    _schedule_cancel_recovery = _cancellation_surface._schedule_cancel_recovery
    _cancel_task_intentionally = _cancellation_surface._cancel_task_intentionally
    _unqueue = _cancellation_surface._unqueue
    _report_queued_stop = _cancellation_surface._report_queued_stop
    cancel = _cancellation_surface.cancel
    cancel_for_parent = _cancellation_surface.cancel_for_parent
    has_live_or_queued_children = _cancellation_surface.has_live_or_queued_children
    has_pending_store_children = _cancellation_surface.has_pending_store_children
    open_teardown_fence = _cancellation_surface.open_teardown_fence
    has_fenced_children = _cancellation_surface.has_fenced_children
    release_teardown_fence = _cancellation_surface.release_teardown_fence
    cancel_queued = _cancellation_surface.cancel_queued
    snapshot_teardown_children = _cancellation_surface.snapshot_teardown_children
    commit_identity_sweep_retire = _cancellation_surface.commit_identity_sweep_retire
    snapshot_teardown_children_only = _cancellation_surface.snapshot_teardown_children_only
    cancel_for_teardown = _cancellation_surface.cancel_for_teardown
    retry_owed_teardown_sweeps = _cancellation_surface.retry_owed_teardown_sweeps
    cancel_all = _cancellation_surface.cancel_all
