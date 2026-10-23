"""The sub-agent run record, :class:`SubagentInfo`, and the scope fields it carries.

One mutable record per run, shared by the manager, its coordinators and the gateway.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kiro_crew.agent_sdk.drivers.acp_vocab import InFlightToolTracker

if TYPE_CHECKING:
    from kiro_crew.subagent import (
        CONTEXT_GROUP_LESSONS,
        CONTEXT_GROUP_MEMORY,
        CONTEXT_GROUP_PROJECT,
        ExecutionContext,
        Iterator,
        SubagentDelivery,
        _RunCreditAccounting,
    )


#: Reap reasons that end a run on purpose. A tombstone written with one of these
#: is a neutral "stopped", never a failure, and the same set decides whether the
#: reap-echo arm in ``_run`` and ``_force_reap``'s own record synthesize an error.
_NEUTRAL_REAP_REASONS = frozenset({"user_stop", "parent_end"})


class _BareStateView(MutableMapping[str, Any]):
    """Live ``{toolCallId: ToolCallState}`` view over an :class:`InFlightToolTracker`.

    The sub-agent reads and writes ``_active_tool_calls`` as a map of bare
    :class:`ToolCallState` values (it carries no aux), while the tracker stores
    ``(state, aux)`` tuples shared with ``AcpSessionHandle``. This adapter
    unwraps on read and wraps (with ``aux=None``) on write so the hand-off still
    lives in the one tracker, yet a direct ``view[id] = state`` — used by a test
    staging a stale call, and by nothing in production — persists and is visible
    to membership and to :meth:`InFlightToolTracker.clear`. Reads go straight to
    the tracker's live dict, so a dispatch/result through the tracker shows up
    here with no copy.
    """

    __slots__ = ("_tracker",)

    def __init__(self, tracker: InFlightToolTracker) -> None:
        self._tracker = tracker

    def __getitem__(self, key: str) -> Any:
        return self._tracker.active_calls[key][0]

    def __setitem__(self, key: str, value: Any) -> None:
        self._tracker.active_calls[key] = (value, None)

    def __delitem__(self, key: str) -> None:
        del self._tracker.active_calls[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._tracker.active_calls)

    def __len__(self) -> int:
        return len(self._tracker.active_calls)


@dataclass
class SubagentInfo:
    """Metadata for a running subagent."""

    id: str
    task: str
    started: float = field(default_factory=time.time)
    done: bool = False
    # True for work accepted behind the stagger/concurrency gate but never
    # started. The record returned by ``spawn`` is normally not registered in
    # ``_agents``; a queued stop registers a synthetic copy temporarily so batch
    # settlement can observe it, and keeps this discriminator True so running
    # cancellation and live-resource monitoring do not treat it as executing.
    queued: bool = False
    # Why a ``queued`` record waits: one of the ``QUEUED_REASON_*`` kinds, or ""
    # for a wait the gate did not label (a claim retained across a store outage).
    # ``queued_reason_detail`` is the gate's own sentence for it -- the same text
    # the task store's ``deferred`` event records -- so ``POST /api/spawn`` can
    # relay it verbatim. Both stay "" on a running or terminal record.
    queued_reason: str = ""
    queued_reason_detail: str = ""
    result: str = ""
    result_path: str = ""
    result_truncated: bool = False  # completion-event copy dropped content → summary+path
    error: str = ""
    # Machine-readable identifier for ``error``, forwarded by ``POST /api/spawn``
    # as the response's ``code`` so a client can switch on the decision instead of
    # parsing the prose. Set today only by the unknown-agent refusal
    # (``AGENT_NOT_FOUND_CODE``), which is the one rejection a client acts on
    # differently: ``spawn_run`` stops re-posting a name the gateway already
    # refused. The other kinds that DO carry an error (bad task, low memory, a cwd
    # refusal, governance) stay un-coded and the handler answers them under a
    # generic code — a code with no consumer would be contract surface bought for
    # nothing. A capacity refusal never reaches this field at all: it returns no
    # record, and the handler answers 429 from that absence.
    error_code: str = ""
    parent_session_key: str = ""
    memory_mode: str = field(default="persistent", kw_only=True)
    _memory_mode_ready: bool = field(default=True, init=False, repr=False)
    agent: str = ""
    # The caller's display name for this run (``spawn_run``'s ``label``), already
    # normalized to one bounded line; "" shows the task text instead. Display
    # only: the run's prompt is ``task``.
    label: str = ""
    # The app that spawned this child (empty for a non-app spawn). Persisted so
    # the child's per-tool-call gate can resolve the app's Level-2 profile, not
    # just the spawn-time decision — without it the profile constrains only
    # whether the spawn was allowed, and the child's ongoing tool calls run
    # unconstrained by the app scope.
    app: str = ""
    approval_mode: str = ""  # "auto" to skip tool approvals in the subagent session
    silent: bool = False  # suppress completion notification (dashboard + Slack)
    turns: int = 0
    last_tool: str = ""
    tool_count: int = (
        0  # count of observed tool calls (incl. auto-approved); drives running-card progress
    )
    last_activity: float = field(
        default_factory=time.time
    )  # time.time() of last stream event; drives idle-stall detection
    stalled: bool = (
        False  # True while the reaper has flagged this subagent as idle/stalled (UI signal)
    )
    # follow_up delivery mode (spawn_steer mode="follow_up"): messages queued
    # here are NOT injected into the running turn — they are dispatched as ONE
    # continuation on this run's conversation after the run completes, so a
    # correction can wait for the current turn instead of interrupting it
    # mid-execution. Drained by the per-run watcher (_deliver_followups).
    pending_followups: list = field(default_factory=list)
    # True once a followup watcher task is armed for this run (one per run).
    _followup_watcher: bool = False
    _stall_suspect_at: float = (
        0.0  # first reaper sweep that saw the idle threshold exceeded; 2-sweep confirmation (scale dampening)
    )
    # True while blocked on a human approval prompt — either the pre-execution
    # spawn gate or a mid-run tool prompt; exempt from idle-stall. Paired with
    # ``_exec_started is None`` it also tells the reaper which of the two a
    # parked run is sitting on.
    _awaiting_approval: bool = False
    # The in-flight tool hand-off rule, shared with ``AcpSessionHandle`` via
    # ``InFlightToolTracker``. The sub-agent tracks its calls by toolCallId: a
    # native child's call (``sub_session_id`` set) is judged on arrival (so a
    # stall is attributed to the child's own command) but not stored, because
    # its result never reaches this loop; a call with an id is stored until its
    # terminal result and the judged slot passes to the newest one still running
    # when it finishes. The attribution lets the liveness oracle key evidence to
    # THIS subagent's own child process (by cmdline match) instead of to the
    # whole runtime subtree — which, on a session-shared runtime, is dominated
    # by kiro-cli's own background I/O.
    # ``_inflight_tool`` (the most recently dispatched call still in flight,
    # None when none is) and ``_active_tool_calls`` (every dispatched call by
    # toolCallId until its terminal result, so a fast call returning first does
    # not drop a slower one still running beside it) are tracker-backed
    # properties below, not fields — the hand-off rule lives in the tracker.
    _tool_tracker: InFlightToolTracker = field(default_factory=InFlightToolTracker)
    # Per-agent liveness oracle. One instance PER AGENT is required, not one per
    # manager: the oracle keys its counter samples by kind ("io"/"cpu"), not by
    # pid, so a shared instance would let one agent's sample become another's
    # baseline and read as movement. Retired (not cleared) on every new tool
    # dispatch so a walk still running against the previous tool cannot write
    # into the next tool's baseline.
    _stall_oracle: Any = None
    # The in-flight offloaded consult for this agent, if any. Tracked so at most
    # ONE /proc walk per agent is outstanding: a permanently wedged read would
    # otherwise leave a blocked worker behind on every reaper sweep and starve
    # the shared subprocess pool that teardown also draws from. Deliberately NOT
    # cleared when the oracle is retired on a new tool dispatch — dropping the
    # handle would un-bound exactly that growth.
    _consult_future: Any = None
    # Monotonic generation of the attribution snapshot above. Bumped on EVERY
    # retirement (new dispatch, final tool result, fresh stream activity) so an
    # offloaded consult that outlived the tool it was submitted for can be
    # recognised as stale and discarded instead of applied to whatever is in
    # flight now. Without it the ``/proc`` walk's own latency is enough to flag a
    # subagent that resumed work while the walk was still running.
    _stall_gen: int = 0
    # Batch/wave identity: set when this spawn is part of a multi-task wave
    # (spawn_run tasks=[...]) so scale plumbing can digest completions and
    # emit batch lifecycle events. Empty for standalone spawns.
    delegation: dict[str, str] = field(default_factory=dict)
    batch_id: str = ""
    batch_total: int = 0
    # True when this member's per-agent injection was HELD for the wave digest
    # (gateway _subagent_done). The run loop must then SKIP mark_delivered():
    # the result is not yet in the parent's context, and a "delivered"
    # tombstone would exclude it from orphan reconciliation — a gateway
    # restart mid-wave would silently lose every held completion. The gateway
    # marks held members delivered when the digest fires.
    _digest_held: bool = False
    # ``time.time()`` when the gateway HELD this member's delivery for the wave
    # digest; 0.0 once that hold has been flushed (or never held). Separate from
    # ``_digest_held`` on purpose: that flag is the restart-safety contract read
    # by the run loop, and must not be mutated by the hold-deadline sweep. This
    # timestamp is the sweep's only input — see ``_sweep_digest_holds``. It is a
    # ``time.monotonic()`` reading, compared with the reaper's monotonic ``now``.
    _digest_held_at: float = 0.0
    # True ONLY for the synthetic record that :meth:`force_digest_flush`
    # announces to release held results whose hold deadline expired. It is NOT a
    # wave member: it carries the wave's ``batch_id`` so the gateway can find
    # the wave's digest buffer, but the gateway must skip every per-member side
    # effect for it (WS terminal event, orchestration tracker accounting,
    # done/ok/err counters, digest lines) and only force the flush.
    _digest_flush_only: bool = False
    # A reap/stop has STARTED but may still be in its (awaiting) teardown. Split
    # out of `reaped` because that flag carries two incompatible meanings: the
    # cancel-recovery scheduler needs it set BEFORE the teardown awaits (or it
    # respawns the run being killed), while `_run`'s error synthesis needs it
    # still False until the reaper actually owns the record (or a run woken by
    # the reaper's session reset skips its own error and reports a FALSE
    # SUCCESS). One flag cannot be both early and late; this one is the early
    # half — "do not respawn, a reap is in flight".
    _reap_started: bool = False
    # WHY the reap in flight is happening, written next to ``_reap_started`` and
    # read by the run loop when its stream dies UNDER that reap. ``_force_reap``
    # tears the run's session down before it cancels the task, so the in-flight
    # turn observes its own runtime being killed first and raises
    # ``AcpProcessDied`` -- "killed (provider shutdown)". Without these two fields
    # that echo was recorded as the run's failure: a ``cause="error"`` tombstone
    # carrying the death text and an ERROR log, for a run a user had just pressed
    # Stop on (or a parent end had cancelled). ``_reap_reason`` is the tombstone
    # cause the reap itself would write (``user_stop`` / ``parent_end`` /
    # ``reaped`` / ``startup_timeout``); ``_stop_origin`` is the one-line WHO/WHY
    # for the record and the log ("stopped by user", "parent conversation ended
    # (retire_kiro_identity_sessions)", "reaped after 900s (reaped)").
    _reap_reason: str = ""
    _stop_origin: str = ""

    def __post_init__(self) -> None:
        # Every way into a record (a spawn, the queue drain, task-store recovery)
        # holds the label to the one bounded display line the task box shows.
        # Imported here: this class's methods run on the facade's globals.
        if self.label:
            from kiro_crew.spawn_label import normalize_spawn_label

            self.label = normalize_spawn_label(self.label)

    @property
    def stop_is_neutral(self) -> bool:
        """Whether the reap that owns this run is a deliberate stop, not a failure.

        Decided by the FIRST stopper (``_reap_reason``), never by
        ``user_stopped`` alone: a Stop that lands while a deadline reap is already
        tearing the run down sets ``user_stopped`` too, and reading that flag would
        let the late Stop convert a claimed deadline failure into a neutral stop.
        A user Stop and a parent end are neutral; a deadline or
        startup reap is the run's own failure.
        """
        return self._reap_reason in _NEUTRAL_REAP_REASONS

    @property
    def _inflight_tool(self) -> Any:
        """The :class:`ToolCallState` a stall verdict is read against (``None``
        when no tool is in flight).

        A view onto the shared :class:`InFlightToolTracker`'s current judged
        call, so the hand-off rule lives in one place
        (``_note_tool_dispatch`` / ``_note_tool_result`` drive it). The setter
        stages one call directly under the empty-id slot, which is what a test
        or a caller that only has a single call to pin uses.
        """
        return self._tool_tracker.current

    @_inflight_tool.setter
    def _inflight_tool(self, value: Any) -> None:
        if value is None:
            self._tool_tracker.clear()
        else:
            self._tool_tracker.dispatch("", value)

    @property
    def _active_tool_calls(self) -> MutableMapping[str, Any]:
        """The live ``{toolCallId: ToolCallState}`` map of calls still running.

        A :class:`_BareStateView` over the shared tracker, so
        ``_note_tool_result``'s successor hand-off and the tests that read
        membership or stage a stale call keep working. The tracker stores
        ``(state, aux)`` tuples; this unwraps to the bare ``ToolCallState`` the
        sub-agent reads and writes. Prefer ``_note_tool_dispatch`` /
        ``_note_tool_result`` / ``_clear_tool_dispatch`` to mutate it.
        """
        return _BareStateView(self._tool_tracker)

    # Set by the gateway on the wave's FINAL member only: terminal snapshots
    # whose delivery tombstones must be settled once the digest has been
    # successfully handed off (i.e. after _on_done returns without raising —
    # the same contract as the per-agent mark_delivered). Settling these at
    # digest COMPOSITION would re-open the restart-loss window between
    # composing and routing.
    _digest_settle_deliveries: list[SubagentDelivery] = field(default_factory=list)
    # True on the synthetic record of a memory-wait expiry whose report the store
    # owes until it reaches the parent (``finish(..., report_owed=True)``). The
    # gateway reads it to carry that debt with a digest or a queued announce
    # (``SubagentDelivery.report_owed``); the report's done-callback clears the
    # store mark itself only on ``_report_delivered``.
    _report_owed: bool = False
    # True once the route ``_on_done`` took has positively delivered this report:
    # the parent's turn consumed the injection, the blocking tool collected it
    # inline, or the route has no conversation and its notification is the whole
    # delivery. A route that parks the report (a digest hold, a queued announce)
    # or gives up on it leaves this unset, and so does any route added later that
    # does not say so; a memory-wait expiry's owed mark is cleared only on it.
    _report_delivered: bool = False
    # True once an injection of this report was given up on: every attempt the
    # gateway made failed, so at most a failure notice was queued for the
    # parent's next turn (``notify_injection_failed``). ``_on_done`` still
    # returns, so this is the only sign the parent was never told, and the run
    # loop writes no ``delivered`` tombstone while it is set.
    _report_undelivered: bool = False
    # True when the gateway QUEUED this completion's injection because the
    # parent's slot was busy. Delivery is not consumption: the announce sits in
    # the slot queue until a turn drains it, and that wait is bounded only by the
    # turn ceiling — far longer than agent.subagent_result_ttl_secs. The run loop
    # must therefore SKIP mark_delivered() (a "delivered" tombstone starts the
    # retention clock, so the reaper would prune result.txt while the promise of
    # it is still queued, and the parent would be handed a dead path). The drain
    # settles the tombstone instead — see
    # ``_ChatSlot.take_pending_subagent_deliveries``.
    _delivery_queued: bool = False
    max_turns: int = 0
    reaped: bool = False
    streaming_text: str = ""
    elapsed: float = 0.0
    # Deadline captured when execution starts (``0`` until then). A config
    # reload changes the manager default for future runs only; it never
    # rewrites this run's deadline.
    timeout_secs: int = 0
    # Cumulative across every attempted turn, including transient retries that
    # consumed credits before failing. Providers that do not bill in credits
    # report zero through the shared TurnUsage contract.
    credits: float = 0.0
    # Shared with terminal reporters so a reap need not wait for a cancelled
    # consumer's state-write drain before settling the active attempt.
    _credit_accounting: _RunCreditAccounting | None = field(default=None, repr=False, compare=False)
    _raw_task: str = ""  # unredacted task for kiro-cli execution prompt
    # CC-specific overrides (ignored for ACP)
    model: str = ""
    # The model id the live session ACTUALLY resolved to serve, read back from
    # the provider's public ``served_model`` accessor. Distinct from ``model``,
    # which is only the REQUESTED pin (often "" ⇒ provider default): the ACP
    # backend reports the served id even on the default, and a routing/config/
    # availability downgrade makes the two differ. "" means unknown/inconclusive
    # (never a wildcard) — the ACP session/new response fills it at spawn, while
    # the CC/raw path only knows it after the first turn, so it is refreshed at
    # completion too. Surfaced on the subagent WS frames and completion meta so a
    # model-pinned review's actual model is auditable.
    resolved_model: str = ""
    # The EFFECTIVE requested model — the per-spawn pin (``model``) OR, when that
    # is empty, the ``agent.role_models['subagent']`` config pin
    # (docs/system-specs/common/model-selection.md names
    # the config pin as *the* way to pin a subagent model). This is the side the
    # downgrade comparison must use: a config-pinned run served a different model
    # is exactly the "unverifiable pin" this feature exists to catch, and keying
    # off the bare per-spawn ``model`` would miss it.
    # ``"auto"`` ⇒ unpinned (no per-spawn pin, no role pin — the provider picks
    # the model). Resolved once at spawn.
    requested_model: str = ""
    # The model ``model.route`` picked for an UNPINNED start and the tier it ran
    # at; both "" when the start was not routed. Kept apart from ``model``, which
    # is only ever a caller's own pin, so a pinned start is never routed, a
    # recovery respawn of a routed start keeps the routing it already has, and
    # the usage row names the model that actually served (``model_source``)
    # rather than a routed id the child session may have withheld.
    routed_model: str = ""
    routed_tier: str = ""
    # Per-call reasoning-effort override (spawn_run ``reasoning_effort``).
    # Wins over the ``role_efforts['subagent']`` pin; ``""`` defers to it.
    # Like ``model``, a non-empty value forces the dedicated-process path.
    reasoning_effort: str = ""
    # Per-call backend (spawn_run ``backend``): the harness this run starts on
    # instead of the gateway default. ``None`` = the default; ``""`` is Kiro's
    # own id. Like ``model``, a value forces the dedicated-process path -- the
    # shared runtime is the PARENT's process, on the parent's backend.
    backend: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    bare: bool = False
    # Continuable conversations (spawn_run keep=True / spawn_continue):
    # keep=True forces a dedicated (non-shared) session, persists the sid via
    # SessionManager.mark_continuable, and skips session-file deletion at
    # teardown so the conversation can be resumed by a later run.
    keep: bool = False
    # Which switchable context groups this sub-agent inherits from the injected
    # session context. All True ⇒ byte-identical to a non-sub-agent session. A
    # parent opts one out when it can name why this task cannot need it; the
    # sub-agent is told by name what was withheld so it reports the gap instead
    # of guessing. Resolved once at spawn and carried through the queue and
    # retry paths, so a drained or retried run sees the same scope as the run
    # its caller asked for.
    include_memory: bool = True
    include_lessons: bool = True
    include_project: bool = True
    # The memory silo this child reads and writes, or "" for the global store.
    # Carried rather than derived: `agent` above holds a kiro-cli modeId, a
    # namespace disjoint from cfg.agents, so resolving a store from it answers
    # `default` for exactly the crew that configured otherwise — silently, and
    # toward the operator's own memory. The caller passes a name it already
    # resolved (ResolvedBindings.memory_store_name) or nothing at all.
    #
    # Empty is the correct default for a plain spawn: a child that inherits no
    # crew reads the global store, which is what every spawn did before crews
    # had silos.
    memory_store: str = ""
    execution_context: ExecutionContext | None = field(default=None, kw_only=True)
    # A named member remains the conversation owner during a template override.
    crew: str = field(default="", kw_only=True)
    # Session key override for continuation runs: a spawn_continue run reuses
    # the ORIGINAL run's session key (``subagent:<conv-id>``) so get_or_create
    # finds the persisted sid and arms session/load. Empty ⇒ the default
    # ``subagent:{id}``.
    conversation_key: str = ""
    # Successor claims, owned by ``SubagentManager.claim_retry`` and the
    # continuation entries. ``_retried_as``: the run a dashboard retry started
    # from this one, or ``SUCCESSOR_PENDING`` while that start is in flight.
    # ``_continued_as``: the FIRST ``spawn_continue`` of this run that landed,
    # never overwritten. ``_continuation_starting``: a continuation start is in
    # flight, set and cleared with the conversation id it claims in
    # ``SubagentManager._continuations_starting``; ``claim_retry`` reads it. A
    # failed card stays failed for history, so these are what stop a retry from
    # running its task beside another successor.
    _retried_as: str = ""
    _continued_as: str = ""
    _continuation_starting: bool = False
    # Optional subprocess cwd override. When set, the subagent kiro-cli/claude-code
    # process launches here instead of the default ``subagent_<id>`` sandbox, so
    # cwd-relative resource globs (``.kiro/steering/**/*.md``, ``AGENTS.md``,
    # ``CLAUDE.md``) resolve against this directory. Validated on spawn against
    # ``AgentConfig.subagent_cwd_allowed_roots``.
    cwd: str = ""
    _pid: int | None = None  # PID of kiro-cli child process, for tombstone diagnostics
    # Wall-clock (time.time) when _run_inner actually began executing. Distinct
    # from ``started`` (set at registration): a subagent may sit in ``_agents``
    # awaiting spawn approval for an arbitrary time before execution begins. The
    # startup watchdog measures from THIS timestamp so it never reaps an agent
    # that is merely waiting for approval. None until execution starts.
    _exec_started: float | None = None
    # ``_exec_started`` on the monotonic clock, stamped and cleared beside it.
    # When it is set the startup watchdogs measure from it, so a wall-clock step
    # cannot reap a start in progress.
    _exec_started_mono: float | None = None
    # The run's registration on the monotonic clock. The reaper measures the run
    # timeout from it, so a wall-clock step cannot reap a healthy run or spare a
    # stuck one; ``started`` stays wall-clock for display and state.json.
    _started_mono: float = field(default_factory=time.monotonic)
    # ``last_activity`` on the monotonic clock, written beside it. The
    # first-prompt-silent watchdog measures from it; ``last_activity`` stays
    # wall-clock for the dashboard's idle display and the stall flag.
    _last_activity_mono: float = field(default_factory=time.monotonic)
    # Wall-clock moment this execution's own session first answered its prompt
    # (``SubagentManager._leave_startup``): the first frame addressed to this
    # session, or a dependency verdict on the prompt. None until then, and reset
    # by ``_run_inner`` for every execution. The startup watchdog and the
    # in-startup bound read it only while ``_pid`` is None, because a runtime
    # PID ends startup on its own; an AcpRuntime-backed start records the PID
    # its runtime reports before the stream opens, and a runtime reports one
    # from spawn until its process is reset. The adaptive controller reads it
    # as the floor of this run's progress.
    _first_stream_started: float | None = None
    # The same moment on the monotonic clock, and the ``_rss_generation`` of the
    # process it belongs to: the reserve's time-based settle reads these, so a
    # wall-clock step or a respawned process never settles a warming row.
    _first_stream_mono: float | None = None
    _first_stream_generation: int = -1
    # ``runtime_global`` frames this execution received while still in
    # startup; named in the startup reap's error. Reset with the marker.
    _startup_cotenant_frames: int = 0
    # (start clock, deadline) the startup watchdog fixed for this start.
    _startup_deadline_stamp: tuple[float, int] | None = None
    # Wall-clock moment this run began waiting for a start-queue permit
    # (``_gate_wait_mark``); None outside that wait. While set, the startup
    # watchdog reads the start clock as paused at this moment: time queued for a
    # permit is not start time. Cleared by ``_gate_exit_reset`` at acquisition,
    # which adds the wait to ``_start_queue_wait_ms``.
    _gate_wait_started: float | None = None
    # ``_gate_wait_started`` on the monotonic clock, marked and cleared beside it.
    _gate_wait_started_mono: float | None = None
    # Learned-cost high-water marks (dynamic-subagent-sizing.md §4.1), sampled
    # periodically by the reaper loop and folded into the cost store at exit.
    peak_rss_gb: float = 0.0
    peak_cpu_cores: float = 0.0
    # Settled-runtime reading: the first quiet subtree sample of a DEDICATED
    # process after its own session answered, with no tool in flight across the
    # read. What the runtime holds once it is up, before its work grows the tree;
    # persisted as the cost store's ``settled_gb``, which the admission gate's
    # dedicated start projection learns from. ``_settled_rss_generation`` ties it
    # to the process it measured, so a respawn re-captures (cancellation.py).
    settled_rss_gb: float = 0.0
    _settled_rss_generation: int = -1
    # GiB the admission gate reserved this start at: the shared price when it was
    # predicted to share its parent's runtime, else the dedicated projection.
    # ``_startup_memory_reserve_gb`` charges it in full until the row
    # settles. None = priced at the dedicated projection (a row the gate did not
    # build, e.g. a recovered one).
    _start_price_gb: float | None = None
    # True while ``_start_price_gb`` is the SHARED price: the one row a
    # dedicated launch must top up first (``_ensure_dedicated_start_priced``).
    _start_priced_shared: bool = False
    # True while this row waits its turn for the dedicated top-up's re-check;
    # the row holding the turn does not count waiters queued behind it.
    _topup_waiting: bool = False
    # Most-recent sample of the same two signals. The peaks answer "how big can
    # this agent get" (what sizing needs); a live task-manager surface needs "how
    # big is it right now", which a high-water mark cannot express — it never
    # comes back down. Both are written by the same sweep, so exposing the last
    # sample costs no extra syscalls.
    last_rss_gb: float = 0.0
    last_cpu_cores: float = 0.0
    # Live process/MCP-stub counts of this run's subtree, from the same sweep.
    # ``None`` = not measured yet (or unmeasurable on this platform), which the
    # surface must render as an em dash: a live runtime with "0 processes" is a
    # lie, and it is exactly the reading that made subagent rows look like they
    # carried no MCP stubs at all.
    last_procs: int | None = None
    last_stubs: int | None = None
    _cpu_jiffies_prev: int = 0  # last subtree utime+stime sample (clock ticks)
    _cpu_sample_ts: float = 0.0  # monotonic time of the last CPU sample
    # How many reaper sweeps have measured a non-zero RSS for this run. The
    # spawn guard treats a dedicated worker as still WARMING until it has been
    # seen by two sweeps (an interval apart), so a single reading taken
    # mid-growth is not mistaken for the worker's size (see
    # _startup_memory_reserve_gb).
    _rss_samples: int = 0
    # Bumped when the run gets a NEW process (the cancel-recovery respawn), so
    # an off-loop sweep that read the old process cannot land its reading on
    # the new one: the sweep snapshots this before reading and writes only if
    # it is unchanged.
    _rss_generation: int = 0
    # Session sharing — when True, this subagent runs as a session on the
    # parent's shared AcpRuntime instead of its own process. Cleanup skips
    # release/reset (no entry in SessionManager) and instead calls shutdown()
    # on the _shared_provider directly.
    _session_sharing: bool = False
    _shared_provider: Any = None  # AcpSessionProvider when _session_sharing=True
    # True when the run inherited a remote session environment: its cost samples
    # go to the ``@remote`` bucket.
    _remote_workspace: bool = False
    # ── Turn-resilience state (subagent parity with the main-agent guards) ──
    # True when the user explicitly stopped this agent (DELETE /api/spawn/{id}).
    # Renders as a neutral "stopped" terminal state (not an error) and
    # preserves whatever partial output was streamed.
    user_stopped: bool = False
    # One-shot budget for auto-continue after an UNEXPECTED (non-user, non-
    # shutdown) asyncio cancellation — mirrors the main path's cancel recovery.
    _cancel_retry_used: bool = False
    # A fresh subagent whose selected agent surface already fills the model
    # window can recover only by rebuilding that surface. Retry once on a
    # dedicated runtime, where native skill projection and Tool Search setup run
    # afresh; a second overflow is terminal so a too-large agent cannot loop.
    _context_overflow_retry_used: bool = False
    # One re-run on a fresh runtime after a live backend lost this run's
    # session; a second loss is terminal.
    _session_not_found_retry_used: bool = False
    _force_dedicated: bool = False
    # The run's completed-ending CLAIM, a one-shot token like ``_finalized``:
    # taken synchronously by a whole answer (a successful complete event,
    # post-processed into ``result``) when no stop got there first (``done``,
    # ``_reap_started``, ``user_stopped``). From then on the ending is
    # completed whatever lands: every stop path treats the claim as it treats
    # ``done`` (Stop, a parent end and the reaper do nothing), and a cancel, the
    # shutdown or the deadline only ends the run's tail early. A respawn would
    # re-run finished work and a failure would discard a whole answer.
    _ending_claimed: bool = False
    # Set by the tail for a successful ending a reap in flight got to first:
    # True when a complete event ended the answer, False when the stream just
    # stopped. ``_run`` names the reap's ending from it.
    _answer_finished: bool = False
    # True while a cancelled run is draining an in-flight off-loop state.json
    # write worker (every off-loop writer). _run's
    # unexpected-cancel recovery gate reads it: on Python 3.10 a second outer
    # cancel can deliver that gate BEFORE the drain finishes (wait_for's
    # _cancel_and_wait awaits an interruptible bare future), and scheduling a
    # recovery writer while the worker is live re-opens the stale-overwrite race
    # the drain exists to close.
    _state_drain_active: bool = False
    # Set only on the synthetic marker `_conversation_busy` returns for a
    # conversation held by an abandoned state writer, so the two
    # retention callers can say "still settling a state write" instead of
    # promising a completion event that has already fired. The authoritative
    # record is `SubagentManager._abandoned_state_writers`, which survives
    # `evict_completed_agents` pruning a completed run out of `_agents`.
    _state_writer_abandoned: bool = False
    # True between an unexpected cancellation and the recovery respawn; the
    # _run finally block skips terminal finalization (subagent_done, on_done)
    # while set so the agent is not reported done mid-recovery.
    _recovering: bool = False
    # Ownership token for the one-time TERMINAL REPORT (`subagent_done` +
    # `_on_done`), claimed via `SubagentManager._claim_finalize`.
    _finalized: bool = False
    # Ownership token for the one-time SLOT RELEASE (`_running_count` decrement
    # + queue drain), claimed via `SubagentManager._release_slot`. Separate from
    # both `reaped` and `done`: whichever terminal path arrives first frees the
    # slot exactly once, so neither a reap that loses the report claim nor a
    # `_run` that sees `reaped` can leave `_running_count` inflated. At
    # 60-100 concurrent agents, a leaked slot starves the queue.
    _slot_released: bool = False
    # Generation the durable task row was claimed under (``kiro_crew.taskq``).
    # Every store write the run makes carries it, so a late write from a
    # superseded dispatch of the same id is fenced out. 0 = no store row.
    _taskq_generation: int = 0
    # Scheduler-core fields (session-start gate, lane-slot waits; see
    # docs/system-specs/modules/subagent.md § Lane-slot waits).
    # Time this start spent queued at its start queues in total, ms (the paused
    # part of the startup clock); 0 when every queue was free.
    _start_queue_wait_ms: float = 0.0
    # ``_start_queue_wait_ms`` measured on the monotonic clock, accumulated beside
    # it; the startup watchdogs subtract it from a span measured on that clock.
    _start_queue_wait_mono_ms: float = 0.0
    # True once the durable row was written ``running`` -- at the FIRST stream
    # event addressed to the run's own session, not at execution start, so a row is never
    # ``running`` while the session is still being created (RFC §4.4).
    _taskq_running_marked: bool = False
    # Provider built by a StartCollector's late adoption, handed to the run
    # that was waiting for it; None otherwise.
    _late_start_provider: Any = None
    # The live WaitRecord (as a dict) while this run has yielded its lane
    # slot for a wait; None while it holds a slot or has none to hold.
    _wait_record: Any = None
    # Set when the run's lane slot was yielded for a wait and a resume entry
    # is queued in admission; cleared when the slot is granted back.
    _resume_pending: bool = False
    # The run loop's wake-up for a yielded slot: set by ``resume_grant`` when
    # the pump hands the slot back, or by the dependency coordinator's
    # ``on_fail`` when the wait ended in failure (``_wait_failed`` names why).
    # None while the run holds its slot.
    _resume_event: Any = None
    _wait_failed: str = ""
    # The wake-up for a start released from the spawn-approval prompt and
    # waiting to be metered into startup by the pump (``_admit_released_start``).
    # None except during that wait.
    _start_release: Any = None
    # True once the terminal report's `_on_done` injection has RETURNED, i.e.
    # the outcome actually reached the parent. Distinct from `_finalized` (the
    # claim, taken before delivery is attempted) and from the "delivered"
    # tombstone (written later, after teardown). Read by `cancel_all()` to tell
    # a report cancelled BEFORE delivery — which must be made recoverable on the
    # next start — from one cancelled AFTER it, which must not be re-delivered.
    _reported_to_parent: bool = False
    # Set by the run's teardown when its kill left the process standing and no
    # tombstone exists yet (a completed run). The terminal report reads it: a
    # delivery the parent acknowledged writes the ``delivered`` tombstone with
    # ``leaked_process`` so the next start ends the process without a notice.
    _leaked_process: bool = False
    # The run's final ACP ``stop_reason`` and its ``classify_stop_reason``
    # class (a ``STOP_CLASS_*`` value), recorded by ``_run_inner`` on the
    # completion that ended the run
    # and carried on the ``subagent_done`` event so the parent sees WHY the run
    # ended, not only whether ``error`` is set. Empty until the run completes.
    stop_reason: str = ""
    stop_class: str = ""
    # True when the delivered ``result`` is a PARTIAL: text streamed before a
    # non-success completion (stall, cancel, error). The parent must not read
    # it as a finished answer.
    partial: bool = False
    # Continue-nudges already spent recovering a ``stalled`` / ``recovering``
    # completion in place (``STOP_RECOVERY_MAX_RETRIES`` budget, shared with
    # the main chat's ``slot._tool_stall_retries``).
    _stop_recovery_used: int = 0

    @property
    def made_no_tool_calls(self) -> bool:
        """Did this run neither emit a ``tool_call`` frame nor raise a permission request?

        The pair the replay gates already read as "no side effect happened":
        ``tool_count`` counts ``tool_call`` frames (and approved child
        escalations) and ``turns`` counts the run's own permission requests, so a
        backend that surfaces a gated tool only as a permission request still
        counts as having called a tool. Such a run wrote no file, ran no command
        and made no commit, whatever its text reports.
        """
        return self.tool_count == 0 and self.turns == 0

    @property
    def outcome(self) -> str:
        """Canonical three-way terminal outcome: 'stopped' | 'failed' | 'completed'.

        THE single source of truth for terminal-state classification. Consumers
        MUST use this (or the ``outcome`` field carried on every subagent_done
        emission) instead of re-deriving from ``error``-nullability — the
        legacy ``error ? failed : completed`` idiom silently misreports a
        user-stopped agent as completed. ``stopped``/``error`` remain on the
        wire for compatibility.
        """
        if self.user_stopped:
            return "stopped"
        if self.error:
            return "failed"
        return "completed"


# Callback: (subagent_info) -> None
AnnounceCallback = Callable[[SubagentInfo], Awaitable[None]]


def _context_groups_of(info: "SubagentInfo") -> frozenset[str]:
    """The switchable context groups this run KEEPS.

    One source of truth for the run's scope, shared by the ``build_message``
    call that applies it and the ``state.json`` record a continuation reads it
    back from, so the two cannot drift.
    """
    return frozenset(
        group
        for group, on in (
            (CONTEXT_GROUP_MEMORY, info.include_memory),
            (CONTEXT_GROUP_LESSONS, info.include_lessons),
            (CONTEXT_GROUP_PROJECT, info.include_project),
        )
        if on
    )


def _context_groups_field(info: "SubagentInfo") -> str:
    """``state.json`` encoding of the run's scope: comma-joined, sorted."""
    return ",".join(sorted(_context_groups_of(info)))
