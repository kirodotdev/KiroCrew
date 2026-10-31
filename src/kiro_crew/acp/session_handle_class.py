"""``class AcpSessionHandle``: the session handle's constructor, state, properties and members.

The class body binds each member an owner module defines (``prompt = _sh_turn.prompt``) and
keeps the constructor, the properties and the class default. ``own_methods`` names each bound
function after its class attribute. ``kiro_crew.acp.session_handle`` re-exports the class,
which keeps that module as its ``__module__``: the facade is the import path callers, pickles
and reprs name.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from kiro_crew._facade_bindings import own_methods
from kiro_crew.acp import kas_wire
from kiro_crew.acp import session_handle_compaction as _sh_compaction
from kiro_crew.acp import session_handle_dispatch as _sh_dispatch
from kiro_crew.acp import session_handle_elicitation as _sh_elicitation
from kiro_crew.acp import session_handle_kas as _sh_kas
from kiro_crew.acp import session_handle_lifecycle as _sh_lifecycle
from kiro_crew.acp import session_handle_mcp as _sh_mcp
from kiro_crew.acp import session_handle_model_selection as _sh_models
from kiro_crew.acp import session_handle_native_children as _sh_children
from kiro_crew.acp import session_handle_permissions as _sh_permissions
from kiro_crew.acp import session_handle_steering as _sh_steering
from kiro_crew.acp import session_handle_turn as _sh_turn
from kiro_crew.acp import session_handle_updates as _sh_updates
from kiro_crew.acp import session_handle_watchdog as _sh_watchdog
from kiro_crew.acp._dispatch import MAX_ACTIVE_TOOL_CALLS
from kiro_crew.acp.elicitation import ElicitationRequest
from kiro_crew.acp.liveness import (
    InFlightToolTracker,
    InteractiveClassification,
    LivenessOracle,
    ToolCallState,
)
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.session_handle_lifecycle import _CANCEL_GRACE_SECS
from kiro_crew.acp.session_handle_runtime_contract import AcpRuntimeProtocol
from kiro_crew.acp.session_handle_watchdog import (
    _WORKING_NEVER_LOGGED,
    WatchdogSettings,
    _load_watchdog_settings,
)
from kiro_crew.acp.skill_reads import SkillReadTracker
from kiro_crew.acp.types import (
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STEER_RETRACT,
    ACP_BACKENDS_STEERING_REQUEST,
    AcpEvent,
    AcpPromptStats,
    JsonRpcMessage,
    steering_advertised,
)
from kiro_crew.recovery.ladder import InfraError

#: The owner modules whose functions ``AcpSessionHandle`` binds as its members, this
#: shell included.
_MEMBER_OWNERS = frozenset(
    {
        "kiro_crew.acp.session_handle_class",
        "kiro_crew.acp.session_handle_compaction",
        "kiro_crew.acp.session_handle_dispatch",
        "kiro_crew.acp.session_handle_elicitation",
        "kiro_crew.acp.session_handle_kas",
        "kiro_crew.acp.session_handle_lifecycle",
        "kiro_crew.acp.session_handle_mcp",
        "kiro_crew.acp.session_handle_model_selection",
        "kiro_crew.acp.session_handle_native_children",
        "kiro_crew.acp.session_handle_permissions",
        "kiro_crew.acp.session_handle_steering",
        "kiro_crew.acp.session_handle_turn",
        "kiro_crew.acp.session_handle_updates",
        "kiro_crew.acp.session_handle_watchdog",
    }
)


class AcpSessionHandle:
    """Handle for a single ACP session on a shared runtime.

    Owns one sessionId + asyncio.Queue. Reads events from the queue (fed by
    AcpRuntime's reader task) and provides prompt/cancel/approve/reject API.
    """

    # The facade is the class's import path, so pickles and reprs keep naming it.
    __module__ = "kiro_crew.acp.session_handle"

    # Fields a bound member assigns first, outside ``__init__``: declared here for type
    # checkers only, so a handle built with ``__new__`` still reads them through the
    # ``getattr`` defaults their readers pass.
    _compaction_terminal_seen: bool
    _kas_compact_turn: bool

    # The session's stale/stall watchdog: ``kiro_crew.acp.session_handle_watchdog``.
    parked_for_secs = _sh_watchdog.parked_for_secs
    _end_human_wait = _sh_watchdog._end_human_wait
    note_keepalive = _sh_watchdog.note_keepalive
    _runtime_idle_secs = _sh_watchdog._runtime_idle_secs
    is_responsive = _sh_watchdog.is_responsive
    rebind_watchdog = _sh_watchdog.rebind_watchdog
    _retire_liveness_state = _sh_watchdog._retire_liveness_state
    _consult_oracle_offloaded = _sh_watchdog._consult_oracle_offloaded
    _working_tool_bound_reached = _sh_watchdog._working_tool_bound_reached
    _log_working_deferral = _sh_watchdog._log_working_deferral
    _emit_watchdog_metric = _sh_watchdog._emit_watchdog_metric
    _end_stalled_tool = _sh_watchdog._end_stalled_tool
    _input_wait_status = _sh_watchdog._input_wait_status
    # The session's MCP servers: ``kiro_crew.acp.session_handle_mcp``.
    _remote_flat_launches = _sh_mcp._remote_flat_launches
    record_mcp_launches = _sh_mcp.record_mcp_launches
    queued_frame_count = _sh_mcp.queued_frame_count
    _owns_mcp_frame = _sh_mcp._owns_mcp_frame
    _accept_oauth_request = _sh_mcp._accept_oauth_request
    pop_pending_oauth_requests = _sh_mcp.pop_pending_oauth_requests
    mcp_session_report = _sh_mcp.mcp_session_report
    wait_mcp_ready = _sh_mcp.wait_mcp_ready
    wait_for_declared_mcp = _sh_mcp.wait_for_declared_mcp
    _note_mcp_sign_in_status = _sh_mcp._note_mcp_sign_in_status
    _offer_mcp_sign_in = _sh_mcp._offer_mcp_sign_in
    want_mcp_sign_in_named = _sh_mcp.want_mcp_sign_in_named
    _match_mcp_sign_in_ask = _sh_mcp._match_mcp_sign_in_ask
    _sync_mcp_reattempting = _sh_mcp._sync_mcp_reattempting
    _runtime_reattempts_mcp = _sh_mcp._runtime_reattempts_mcp
    _reattempt_failed_mcp_servers = _sh_mcp._reattempt_failed_mcp_servers
    _apply_init_notification = _sh_mcp._apply_init_notification
    drain_init = _sh_mcp.drain_init
    # What KAS speaks beyond ACP on a session: ``kiro_crew.acp.session_handle_kas``.
    discard_unanswered_prompt = _sh_kas.discard_unanswered_prompt
    _is_kas_hooks_request = _sh_kas._is_kas_hooks_request
    _answer_kas_hooks_request = _sh_kas._answer_kas_hooks_request
    _answer_kas_hook_execute = _sh_kas._answer_kas_hook_execute
    _cancel_hook_tasks = _sh_kas._cancel_hook_tasks
    _send_hook_error = _sh_kas._send_hook_error
    _handle_kas_update = _sh_kas._handle_kas_update
    _handle_kas_session_info = _sh_kas._handle_kas_session_info
    _apply_kas_context_pct = _sh_kas._apply_kas_context_pct
    _apply_kas_turn_completion = _sh_kas._apply_kas_turn_completion
    # Harness-native child sessions: ``kiro_crew.acp.session_handle_native_children``.
    _handle_kas_subagent = _sh_children._handle_kas_subagent
    _handle_kas_subagent_chunk = _sh_children._handle_kas_subagent_chunk
    _build_child_tool_activity_prefix = _sh_children._build_child_tool_activity_prefix
    _note_native_child = _sh_children._note_native_child
    _note_native_roster = _sh_children._note_native_roster
    native_child_resume_refusal = _sh_children.native_child_resume_refusal
    report_native_children = _sh_children.report_native_children
    # The session's model: ``kiro_crew.acp.session_handle_model_selection``.
    set_mode = _sh_models.set_mode
    _served_default_after_set_mode = _sh_models._served_default_after_set_mode
    set_model = _sh_models.set_model
    _push_model_config_option = _sh_models._push_model_config_option
    set_config_option = _sh_models.set_config_option
    _advertised_model_ids = _sh_models._advertised_model_ids
    supports_config_option = _sh_models.supports_config_option
    get_valid_effort_levels = _sh_models.get_valid_effort_levels
    _crew_agent_name = _sh_models._crew_agent_name
    store_session_config = _sh_models.store_session_config
    ensure_served_default = _sh_models.ensure_served_default
    _normalize_models = staticmethod(_sh_models._normalize_models)
    refresh_available_models = _sh_models.refresh_available_models
    _mark_available_models_captured = _sh_models._mark_available_models_captured
    _feed_advertised_model_cache = _sh_models._feed_advertised_model_cache
    _persist_advertised_models_if_changed = _sh_models._persist_advertised_models_if_changed
    maybe_refresh_available_models = _sh_models.maybe_refresh_available_models
    _sync_effort_levels = _sh_models._sync_effort_levels
    # Steering a live turn: ``kiro_crew.acp.session_handle_steering``.
    steer = _sh_steering.steer
    clear_pending_steers = _sh_steering.clear_pending_steers
    _steer_via_steering_request = _sh_steering._steer_via_steering_request
    _settle_abandoned_steering = _sh_steering._settle_abandoned_steering
    _take_injected_steers = _sh_steering._take_injected_steers
    _release_proven_steers = _sh_steering._release_proven_steers
    _note_steering_denial = _sh_steering._note_steering_denial
    # The session outside its turn loop: ``kiro_crew.acp.session_handle_lifecycle``.
    _died = _sh_lifecycle._died
    cancel = _sh_lifecycle.cancel
    send_command = _sh_lifecycle.send_command
    command_result = _sh_lifecycle.command_result
    _send_awaited = _sh_lifecycle._send_awaited
    bind_session_key = _sh_lifecycle.bind_session_key
    destroy = _sh_lifecycle.destroy
    _cleanup_transcript = _sh_lifecycle._cleanup_transcript
    cleanup_transcript_files = staticmethod(_sh_lifecycle.cleanup_transcript_files)
    _wait_for_response = _sh_lifecycle._wait_for_response
    # Context compaction: ``kiro_crew.acp.session_handle_compaction``.
    compact = _sh_compaction.compact
    _kas_compact_request = _sh_compaction._kas_compact_request
    _kas_compact_advertised = _sh_compaction._kas_compact_advertised
    _settle_kas_compaction = _sh_compaction._settle_kas_compaction
    _inline_turn_finished_cleanly = _sh_compaction._inline_turn_finished_cleanly
    _compacts_inline = _sh_compaction._compacts_inline
    wait_for_compaction = _sh_compaction.wait_for_compaction
    _drain_post_compaction_metadata = _sh_compaction._drain_post_compaction_metadata
    _track_prompt_usage = _sh_compaction._track_prompt_usage
    _track_metadata = _sh_compaction._track_metadata
    _backfill_context_window = _sh_compaction._backfill_context_window
    _settle_codex_compaction = _sh_compaction._settle_codex_compaction
    _codex_compaction_event = _sh_compaction._codex_compaction_event
    # Permission requests and in-band refusals: ``kiro_crew.acp.session_handle_permissions``.
    approve_tool = _sh_permissions.approve_tool
    _restore_withdrawn = _sh_permissions._restore_withdrawn
    reject_tool = _sh_permissions.reject_tool
    _deny_spec_disabled_tool = _sh_permissions._deny_spec_disabled_tool
    _tripwire_spec_disabled_tool = _sh_permissions._tripwire_spec_disabled_tool
    _refuse_unidentifiable_mcp_approval = _sh_permissions._refuse_unidentifiable_mcp_approval
    _refuse_unreadable_mcp_identity = _sh_permissions._refuse_unreadable_mcp_identity
    _refuse_unretained_provenance = _sh_permissions._refuse_unretained_provenance
    _audit_handle_reject = _sh_permissions._audit_handle_reject
    apply_session_permission_routing = _sh_permissions.apply_session_permission_routing
    _build_permission_event = _sh_permissions._build_permission_event
    _handle_elicitation_request = _sh_elicitation._handle_elicitation_request
    _audit_elicitation_decision = _sh_elicitation._audit_elicitation_decision
    answer_elicitation = _sh_elicitation.answer_elicitation
    cancel_pending_elicitations = _sh_elicitation.cancel_pending_elicitations
    # ``session/update`` frames as ``AcpEvent`` values: ``kiro_crew.acp.session_handle_updates``.
    _report_evicted_attributions = _sh_updates._report_evicted_attributions
    _no_tool_in_flight = _sh_updates._no_tool_in_flight
    _release_settled_tool_call = _sh_updates._release_settled_tool_call
    _emit_tool_interrupted_sel = _sh_updates._emit_tool_interrupted_sel
    _tool_kind_cache_for_wire = _sh_updates._tool_kind_cache_for_wire
    _identity_unreadable_cache_for_wire = _sh_updates._identity_unreadable_cache_for_wire
    _tool_call_cache_overflow_for_wire = _sh_updates._tool_call_cache_overflow_for_wire
    _handle_update = _sh_updates._handle_update
    _structured_status_event = _sh_updates._structured_status_event
    _note_status_rejected = _sh_updates._note_status_rejected
    # One turn end to end: ``kiro_crew.acp.session_handle_turn``.
    wait_turn_done = _sh_turn.wait_turn_done
    prompt = _sh_turn.prompt
    stream_command = _sh_turn.stream_command
    _run_turn = _sh_turn._run_turn
    _close_abandoned_turn = _sh_turn._close_abandoned_turn
    # The per-turn event loop: ``kiro_crew.acp.session_handle_dispatch``.
    _dispatch_events = _sh_dispatch._dispatch_events
    _classify = _sh_dispatch._classify

    def __init__(
        self,
        session_id: str,
        queue: asyncio.Queue[JsonRpcMessage | None],
        runtime: AcpRuntimeProtocol,
        watchdog: WatchdogSettings | None = None,
        crew_agent: str = "",
        session_key: str = "",
        bound_cwd: str = "",
    ) -> None:
        self._session_id = session_id
        # The Kiro Crew session that OWNS this ACP session, threaded from the
        # runtime's create/load paths the way ``crew_agent`` is, and rebound on a
        # warm-pool claim. The hooks execute path keys its listed-id record and
        # its governance resolution by it; the ACP ``sessionId`` a request names is
        # host-supplied and is never used for either. Empty for a pooled session
        # nobody has claimed yet, which the execute path refuses.
        self._session_key = session_key
        self._listed_hooks = kas_wire.ListedHookStore()
        # Strong references to in-flight hook executions: the loop holds only a
        # weak one, and a collected task would leave its request unanswered.
        self._hook_tasks: set[asyncio.Task[None]] = set()
        # Same reason, for the ``session/cancel`` a late ``startedNewTurn`` steering
        # answer sends from a done-callback (see ``_steer_via_steering_request``).
        self._steering_cancel_tasks: set[asyncio.Future[None]] = set()
        # codex ``_session/steering`` answers awaiting settlement by the turn they
        # were aimed at: ``(answer, prompt generation, echo text)``.
        self._steering_answers: list[tuple[asyncio.Future[dict[str, Any]], int, str]] = []
        # Per answer in ``_steering_answers``: resolved True when the dispatch loop
        # settles it inside its turn, False when it is dropped unsettled. The steer
        # call returns this, so True always means "settled before the terminal".
        self._steering_settled: dict[asyncio.Future[dict[str, Any]], asyncio.Future[bool]] = {}
        # Answers still awaited after their turn ended, kept only so a late
        # ``startedNewTurn`` can be cancelled. Counted against the same bound as
        # ``_steering_answers`` and forgotten on ``destroy``, so the runtime's
        # registration of an answer that never comes is bounded too.
        self._abandoned_steering: list[asyncio.Future[dict[str, Any]]] = []
        # Set by a denied approval on a codex turn: nothing injected after it can
        # survive the cancel, so no later answer in the turn settles as consumed.
        self._turn_steering_denied: bool = False
        # Wrapped text of this turn's codex steers proven read before its
        # terminal; reported consumed only if the turn ends cleanly
        # (``_release_proven_steers``).
        self._steers_proven: list[str] = []
        # Wrapped text of this turn's codex steers whose ``steer()`` call has
        # already returned True. Only these are released as consumed: the
        # caller persists the steer's row synchronously on that return, so a
        # consumption report can never clear a pending entry whose row does
        # not exist yet. Matched by identity (``_release_proven_steers``).
        self._steers_accepted: list[str] = []
        # True once this turn's prompt has been written to the adapter. A codex
        # steer before that point reaches an adapter with no turn of ours and is
        # answered ``startedNewTurn``, whose untargeted cancel could then land on
        # the prompt about to go out, so no steer is sent until it is set.
        self._prompt_written: bool = False
        self.native_context_documents: dict[str, str] = {}
        self.native_context_documents_fit: Callable[[], bool] | None = None
        # Whether THIS session's array carries the member session-control entry.
        # Set by the composer that decided it (create and load alike), and read
        # by the context builder so the member operating-mode block is injected
        # only when the session actually holds the tools that block teaches.
        self.member_dispatch_mounted: bool = False
        self._queue = queue
        self._runtime = runtime
        # The directory THIS session was opened against, which on a shared runtime is
        # not the runtime's own: sessions for different projects live on one process.
        self._bound_cwd = bound_cwd
        # When True, destroy() skips the transcript unlink (subagent
        # continuability: the transcript is spawn_continue's resume material).
        self.keep_transcript = False
        self.memory_mode = "persistent"
        # Token carried by THIS session's injected broker-stub entries
        # (``mcp_gateway.claim.mint_stub_session_token``), set by the runtime
        # that created the session. It is what a later claim-push names so
        # gatewayd re-targets this session's stub connections and not every
        # session's on the shared runtime. Empty when the gateway injected no
        # stubs. Never logged: it is a bearer name for this session's identity.
        self.stub_session_token: str = ""
        # Watchdog windows are snapshotted here (construction time) so the
        # dispatch loop never reads config; the liveness oracle carries the
        # per-session evidence state (tracked child, counter samples).
        # ``crew_agent`` is the CANONICAL crew identity resolved by the surface
        # that owns it and plumbed down (see _load_watchdog_settings): it keys
        # the per-agent watchdog_tool_stall_* overrides by direct config lookup.
        # It must NEVER become an OTel metric attribute (free-form =>
        # cardinality bomb; see metrics/schema.py) — telemetry carries the
        # agent_override BOOLEAN. An explicit ``watchdog`` always wins
        # verbatim: the async creation paths (runtime.create_session /
        # load_session) resolve it off-loop and hand it in, so the synchronous
        # load below is only the fallback for direct constructions (tests).
        self._crew_agent = crew_agent
        self._watchdog = watchdog if watchdog is not None else _load_watchdog_settings(crew_agent)
        # How this session launched its stdio MCP servers, recorded by the
        # runtime once the session's array is final (``record_mcp_launches``).
        # The oracle's remote_flat scan reads it to find the in-flight tool's
        # own server process; empty means no server can be found, which keeps
        # the full window.
        self._mcp_launches: dict[str, tuple[tuple[str, ...], ...]] = {}
        self._oracle = LivenessOracle(
            sample_min_secs=self._watchdog.wellness_sample_secs,
            server_launches=self._remote_flat_launches,
        )
        # Keep the executor future, not an await-scoped flag: wait_for can time
        # out while the underlying thread continues its /proc walk. A pending
        # future makes the next watchdog tick answer UNKNOWN instead of
        # submitting a second job, so a wedged walk cannot stack blocked workers
        # in the shared subprocess_executor().
        self._consult_future: asyncio.Future[tuple[str, str]] | None = None
        # The in-flight tool hand-off rule, shared with the sub-agent reaper
        # (``InFlightToolTracker``): parallel calls can finish in either order,
        # so each attribution is retained by toolCallId until its terminal result
        # and the judged call falls back to the newest remaining one. The
        # pre-dispatch interactive classification of a shell call rides as the
        # tracker's aux payload (``None`` for a non-shell tool). ``ToolCallState``
        # does not carry the id, and the ``waiting_input`` status must name the
        # call, so the tracker keeps it too (``_inflight_tool_call_id`` below).
        # Bounded by ``MAX_ACTIVE_TOOL_CALLS``: a backend that emits distinct
        # tool_call frames whose results never report a terminal status would
        # otherwise grow the tracker for the length of the turn. At the cap the
        # OLDEST attribution is evicted and the call just dispatched is always
        # kept, so the oracle keeps reporting the tool actually in flight rather
        # than failing permissive on stall detection.
        self._tool_tracker = InFlightToolTracker(max_calls=MAX_ACTIVE_TOOL_CALLS)
        # toolCallIds that streamed output (a non-final tool_call_update with
        # content) this turn. A command that already produced output may have
        # already acted, so a non-interactive retry of it is never ``safe_retry``.
        self._tool_output_seen: set[str] = set()
        # ``kirocrew/status`` rejections this turn, keyed by reason, so the
        # ``status_rejected`` log line fires once per reason per turn rather than
        # once per frame from a misbehaving emitter.
        self._status_rejected: dict[str, int] = {}
        # Whether the ``waiting_input`` status for the CURRENT stall was already
        # yielded (the ``wait`` policy keeps the turn open across ticks and must
        # not re-emit it every tick).
        self._input_wait_emitted = False
        # Native (harness-internal) child session ids seen on child-routed
        # frames this turn — the residency count at the parent recovery
        # boundary (RFC §14.8). Reset per turn with the roster. Bounded by
        # NATIVE_CHILD_ROSTER_CAP; ids past the cap are counted, not stored.
        self._native_child_sids: set[str] = set()
        self._native_child_overflow = 0
        # Last monotonic ts a WORKING-verdict deferral was logged (rate limit).
        self._working_logged_ts = _WORKING_NEVER_LOGGED
        # Terminal compaction status captured by compact() while draining its
        # own prompt turn (kiro-cli may emit _kiro.dev/compaction/status
        # BEFORE end_turn). wait_for_compaction() consumes it first so the
        # drain never strands a caller into a spurious 120s timeout.
        self._compact_result: dict[str, str] | None = None
        # Credits kiro reported while no turn was running (a compaction
        # waited on between turns). ``_track_metadata`` adds to it and
        # ``_run_turn`` moves it onto the next turn's stats, so it is billed
        # exactly once.
        self._between_turn_credits = 0.0
        # Set when a codex compaction's ``started`` frame is seen inside a turn,
        # cleared by its terminal. It guards TWO directions. A LOADED session
        # replays a past compaction as a ``tool_call`` that is already
        # ``completed``, so only a terminal following a ``started`` seen in THIS
        # turn describes work this turn did -- reading a replayed one as live would
        # reset the context meter against a window nobody just summarized. And a
        # compaction that ERRORS reports nothing at all: codex-acp's ``runCompact``
        # never resolves, so the ``session/prompt`` request goes unanswered, which
        # leaves this armed for ``_settle_codex_compaction`` at the turn's terminal.
        self._codex_compaction_pending: bool | str = False
        self._cancelled = False
        # Unresponsive-cancel tracking (mirrors AcpClient._cancel_ts /
        # _cancel_grace_secs). Set by cancel(); the dispatch loop uses them to
        # unblock the caller if kiro-cli never acks the cancel.
        self._cancel_ts = 0.0
        self._cancel_grace_secs = _CANCEL_GRACE_SECS
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        # Count of prompts this handle has started. A codex steering answer reads
        # it to tell "the turn the steer was aimed at" from a newer one of ours
        # (see ``_steer_via_steering_request``).
        self._prompt_starts = 0
        self._stale_eligible = False
        # Latched on this session's FIRST text chunk or tool_call and never
        # cleared: the registration-throttle death classification is refused
        # once work has been observed, so the transient verdict it hands the
        # retry ladders can only ever license replaying a session that provably
        # did nothing. Per SESSION, not per turn or per process — replay safety
        # is a fact about what THIS session's consumer may re-run, and the
        # process-level fact (a stale throttle line in a shared runtime's ring)
        # must not license replaying a sibling that already acted.
        self._prompt_or_tool_seen = False
        # Set when a genuine stale turn is probed via session/cancel; read by the
        # unresponsive-cancel branch to distinguish a confirmed wedge (signal
        # auto-recovery) from an ordinary unacked cancel (unblock caller).
        self._stale_probe = False
        self._tool_dispatched = False
        self._last_stop_reason = ""
        # The most recent tool result's infrastructure-error classification
        # (``recovery.ladder.InfraError``) or None. Public, like
        # ``last_compaction_transient``: the dashboard reads it through getattr
        # on whichever client class serves the slot.
        self.last_infra_error: InfraError | None = None
        # Monotonically increasing count of NOTIFICATION frames delivered to this
        # session by the shared queue. Incremented in _wait_for_response whenever
        # it consumes a notification (not a response) from the queue while
        # buffering for a concurrent command call. The TOCTOU guard in
        # _dispatch_events snapshots this before the oracle await and compares
        # after: an advance means a real activity frame arrived while the oracle
        # was executing (even if _wait_for_response had consumed it from the
        # queue in the meantime). Pure queue-depth checks cannot see frames that
        # are temporarily held in a concurrent consumer's buffer list.
        self._ingress_seq: int = 0
        # Consumer-park accounting, read by the idle clocks in _dispatch_events
        # and by external observers via parked_for_secs(). `_parked_total` is
        # cumulative for the turn; `_parked_since` is set only while suspended at
        # a yield in prompt().
        self._parked_total: float = 0.0
        self._parked_since: float | None = None
        # Last /api/session-keepalive touch addressed to THIS session (monotonic,
        # 0.0 for none), and `_parked_total` when it landed. Written by
        # note_keepalive(); folded into the tool-idle clock in _dispatch_events.
        # Never reset per turn: a touch from an earlier turn is older than this
        # turn's own frames, so the fold can never pick it up.
        self._keepalive_ts: float = 0.0
        self._keepalive_parked: float = 0.0
        # Monotonic timestamp of a `failed` compaction status seen this turn
        # (None otherwise). Arms the post-failure budget in _dispatch_events,
        # which ends an abandoned turn instead of draining to the ceiling.
        self._compaction_failed_at: float | None = None
        # The request id of a prompt whose consumer left before its turn ended
        # (a cancelled or closed stream), or None. The backend may still be
        # running that turn and writing its frames to this session's queue, so
        # the next turn cancels it and waits for its answer before sending
        # (``_close_abandoned_turn``).
        self._abandoned_prompt_id: int | None = None
        # Retryability of the LAST failed compaction, read by the dashboard's
        # STOP_REASON_COMPACTION_FAILED branch to decide between re-queuing the
        # abandoned message and giving up. Public (no leading underscore)
        # because that consumer reaches it through getattr on whichever of the
        # two client classes is serving the slot. Verdict only — see the twin
        # comment in AcpClient for why the reason text is not forwarded.
        self.last_compaction_transient: bool = False
        # Consumers that implement the low-fidelity child downgrade (dashboard
        # card / interactive approver) opt IN; for everyone else the handle
        # itself fail-closes low-fidelity child permission requests below, so
        # a consumer that predates the fidelity contract can never auto-approve
        # a child on agent-authored context. Full-fidelity child events flow to
        # every consumer unchanged (mode parity everywhere).
        self.child_fidelity_aware: bool = False
        # Consumers that can put an MCP elicitation to a person (the dashboard
        # chat runner) opt IN. For everyone else the handle declines the
        # request itself before it would be yielded, so a consumer that does
        # not read EVENT_ELICITATION_REQUEST can never leave the tool call
        # waiting on an answer nobody will give.
        self.accepts_elicitation: bool = False
        # Request id -> the parsed ask, for every elicitation yielded and not
        # yet answered. answer_elicitation validates against it; the per-turn
        # reset and teardown cancel whatever is left.
        self._pending_elicitations: dict[str | int, ElicitationRequest] = {}
        # User-visible notices for permission requests this handle answered
        # itself (fail-close gate below, pre-turn drain): flushed as
        # EVENT_SUBAGENT_ACTIVITY at the next dispatch so the rejection shows
        # on the child's crew card instead of vanishing into the log.
        self._pending_reject_notices: list[tuple[str, str]] = []
        # In-flight SEL audit tasks for handle-owned permission rejections
        # (fail-close gate, pre-turn drain) — retained so they cannot be
        # garbage-collected mid-flight. Mirrors AcpRuntime._audit_tasks.
        self._audit_tasks: set[asyncio.Task[None]] = set()
        # Set when a permission event is yielded, cleared when it is answered.
        # Distinguishes "waiting for a human" (legitimate, bounded elsewhere)
        # from "the consumer stopped pulling for some other reason".
        self._awaiting_permission: bool = False
        # toolCallId -> redacted input string, written by the shared parser so a
        # later tool result can recover its originating input (mirrors AcpClient).
        self._tool_call_inputs: dict[str, str] = {}
        # Same-key provenance for ``_tool_call_inputs``.  The cached text is
        # intentionally redacted; this bit lets an approval surface fail closed
        # without retaining or forwarding the removed secret bytes.
        self._tool_call_input_redacted: dict[str, bool] = {}
        # toolCallId -> is_shell, cached from the tool_call notification so the
        # later permission_request event (which carries no trusted kind) can
        # inherit the canonical shell signal. Mirrors AcpClient's cache and is
        # the ONLY trusted source build_permission_event reads for is_shell.
        self._tool_call_is_shell: dict[str, bool] = {}
        # toolCallId -> raw structured params (dict) cached from the tool_call
        # notification so the later permission_request event can carry
        # raw_tool_params for the governance keystone (sensitive-path /
        # write-protected-config) checks. Mirrors AcpClient's _tool_call_params.
        self._tool_call_raw_params: dict[str, dict] = {}
        # Skill bodies this session's tool calls read directly, credited to the
        # usage ledger once each read completes. The same tracker AcpClient
        # owns, so a backend served by the shared runtime earns the credit a
        # per-process one does. Same per-turn lifecycle as the caches above.
        self._skill_reads = SkillReadTracker()
        # toolCallId -> path named by the tool_call's diff content block, so the
        # permission event can carry diff_path for the edit gate when the
        # params themselves carry no path key. Same lifecycle as the caches above.
        self._tool_call_diff_path: dict[str, str] = {}
        # toolCallId -> harness-classified ``kind`` from the tool_call frame, so a
        # permission_request that omits ``kind`` still reaches the gate's
        # kind-keyed tiers. Allocated like every sibling cache; whether it is
        # HANDED to the wire parsers is decided per frame by
        # ``_tool_kind_cache_for_wire`` (members of
        # ``ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL`` only), so construction
        # is the same for every harness and a non-member's path is unchanged.
        self._tool_call_kind: dict[str, str] = {}
        # Emitting scopes (sessionIds) whose tool_call frames the per-turn cache
        # admission bound refused this turn (``_dispatch.admit_tool_call_cache_entry``:
        # too many distinct ids, or a frame string over its size bound). Holds
        # fixed-size scope DIGESTS (``overflow_scope_key``), never the scope text
        # -- the scope is a backend-authored string the admission may have
        # refused for its size -- one per scope, never per id, and past the
        # shared entry cap the ``OVERFLOW_EVERY_SCOPE`` marker instead, so the
        # record is bounded whatever the backend sends. Read by
        # ``_refuse_unretained_provenance``: a permission request from a recorded
        # scope whose id misses every cache is REFUSED rather than yielded,
        # because a consumer would otherwise judge a call whose kind, identity
        # and target were never retained. Same lifecycle as the caches above.
        self._tool_call_cache_overflow: set[str] = set()
        # toolCallId -> trusted MCP server name (_meta.kiro.mcpServerName) cached
        # from the tool_call notification so the later permission_request event
        # can carry mcp_server_name (empty on the permission payload). This is
        # what lets hooks.on_tool_call's app-own-server auto-approve fire on the
        # permission path. Mirrors _tool_call_is_shell.
        self._tool_call_mcp_server: dict[str, str] = {}
        # Trusted tool name (_meta.kiro.toolName) cached like _tool_call_mcp_server
        # so the permission event can rebuild mcp__<server>__<tool> for per-tool
        # governance in the app-own-server auto-approve.
        self._tool_call_tool_name: dict[str, str] = {}
        # toolCallId -> the tool's own name its tool_call frame stated, for the
        # permission event's harness_tool_id (see _dispatch.harness_tool_name).
        self._tool_call_harness_tool_name: dict[str, str] = {}
        # Per scoped toolCallId, True when the tool_call frame PRESENTED an MCP
        # identity half the name bound refused (``mcp_identity_unreadable``):
        # the call names a tool Crew cannot retain, and its permission request
        # is refused (``_refuse_unreadable_mcp_identity``) rather than judged
        # under the empty name the identity caches hold. Sparse (written only
        # when True), a sibling of the caches above under the same admission,
        # cleared, evicted and released with them.
        self._tool_call_identity_unreadable: dict[str, bool] = {}
        # Parent-scoped cache keys populated by tagged native-child tool calls.
        # Cleared per turn beside the sibling per-call caches below.
        self._native_child_tool_call_ids: set[str] = set()
        # Server names for which a mid-session MCP OAuth banner was already
        # emitted, so we don't spam duplicates. Discarded on the matching
        # server_initialized / server_init_failure so a later token-expiry
        # retry can re-surface. Instance-scoped (NOT reset per turn) — mirrors
        # AcpClient._oauth_emitted_servers.
        self._oauth_emitted_servers: set[str] = set()
        # OAuth requests collected by drain_init(). Dashboard startup drains
        # this list through AcpSessionProvider after create_session returns.
        self._pending_oauth_requests: list[dict[str, str]] = []
        # Servers whose last ``_kiro/mcp/status`` entry for this session was an
        # authorization failure and that have not connected since. Kept across a
        # timed-out sign-in, whose entry reports ``failedAuthorization`` false.
        self._mcp_sign_in_needed: set[str] = set()
        # Needed servers a user message named and whose sign-in the runtime
        # has not started yet. Being needed alone never starts a sign-in; see
        # ``want_mcp_sign_in_named``.
        self._mcp_sign_in_wanted: set[str] = set()
        # The user message that asked for this turn's sign-ins, matched again
        # once the pre-turn drain or a mid-turn status tracks a server it
        # names, and the servers it already marked wanted. One turn's only:
        # ``_run_turn`` clears both when the turn ends.
        self._mcp_sign_in_ask = ""
        self._mcp_sign_in_asked: set[str] = set()
        # Tracked servers whose status reported ``connected`` and whose
        # completion the dispatch loop has not yet yielded. KAS sends no
        # server-initialized frame for a completed sign-in, so the loop yields
        # ``EVENT_MCP_SERVER_INITIALIZED`` for these itself and the dashboard
        # closes the Authorize banner.
        self._mcp_sign_in_completed: set[str] = set()
        # How many status entries the sign-in tracker last dropped past
        # ``BUCKET_CAP``; a change is logged once, so a steady overflow does not
        # repeat the warning on every snapshot.
        self._mcp_sign_in_dropped = 0
        # Servers whose last status entry for this session was ``failed`` with
        # an error that reads as a missing or refused credential (not an OAuth
        # authorization failure, which the sign-in above handles), and how many
        # times each was re-attempted. Both bounded by ``BUCKET_CAP`` names.
        # See kiro_crew.acp.mcp_reattempt.
        self._mcp_reattempt_waiting: set[str] = set()
        self._mcp_reattempt_counts: dict[str, int] = {}
        # What THIS session's MCP servers reported at init — parity with
        # AcpClient._mcp_report. On the shared runtime the frames are staged
        # per sessionId before this handle's queue exists, so the report is
        # genuinely this session's and not the process's.
        self._mcp_report = McpSessionReport()
        # (server, tool) pairs this session's agent spec switches off, set by the
        # runtime from the mirror's session projection. The wire cannot carry the
        # restriction on a mirrored host -- there is no per-tool deny channel in
        # ``mcpServers`` -- so it is honoured at the permission request instead.
        # Empty for every host whose MCP surface needs no projection, which makes
        # ``_deny_spec_disabled_tool`` a single falsy read on those sessions.
        # Mirrors ``AcpClient._spec_denied_tools``.
        self.spec_denied_tools: frozenset[tuple[str, str]] = frozenset()
        # The parsed agent spec this session's mirrored array was built from, set by
        # the runtime from the same projection. None on a host with no mirror. A
        # member session's loaded-check reads this, never a re-read of the file.
        self.consumed_agent_spec: dict[str, Any] | None = None
        self.mirror_used: bool = False
        # A member session's confirmation result, recorded by the runtime from the
        # mirrored arm: the saved template whose consumed projection matched, plus
        # the projection gaps that keep the Capabilities view "unverified". Both
        # stay at their defaults for a host with no mirror and for a session whose
        # consumed spec matched no saved intent.
        self.confirmed_projection_template: str = ""
        self.capability_projection_gaps: tuple[str, ...] = ()
        # The capabilities the agent batch this session registered auto-approves
        # (see ``kas_agents.projected_auto_approved``); None when no batch was sent.
        self.kas_auto_approved: frozenset[str] | None = None
        # The built-ins that batch keeps at ``ask`` but the active agent's spec
        # grants (``kas_permissions.crew_answered_builtins``): Crew's gate answers
        # their permission requests itself, after the deny floor, the
        # sensitive-path check and the governance ceiling. Empty with no batch.
        self.kas_crew_answered: frozenset[str] = frozenset()
        # The agent's `toolsSettings.shell.allowedCommands` as the gate matches
        # them (``kas_permissions.crew_answered_commands``), answered the same way.
        self.kas_crew_answered_commands: tuple[str, ...] = ()
        # The only shell commands the agent's spec lets run when it sets
        # `toolsSettings.shell.denyByDefault` (``kas_permissions.spec_shell_allowlist``);
        # None when it does not. The gate refuses every other shell call.
        self.kas_spec_shell_allowlist: tuple[str, ...] | None = None
        # The only paths the agent's write tool may touch when its spec sets
        # `toolsSettings.fs_write.fallbackAction: "deny"`, anchored at this
        # session's directory (``kas_agents.projected_write_allowlist``); None when
        # it does not. The gate refuses every other file write.
        self.kas_spec_write_allowlist: tuple[str, ...] | None = None
        # The agent this session runs: the one the batch was registered for, then
        # whichever a KAS mode switch moves it to ("" with no batch). A turn that
        # names no agent runs this one, so its spec hooks are this agent's.
        self.kas_projected_agent: str = ""
        # The agent batch this session registered, so a mode switch can answer
        # what the agent it moves to auto-approves.
        self.kas_registered_agents: list[dict[str, Any]] = []
        # The KAS MCP launch snapshot names every projection of this session wrote
        # (``acp.kas_env_launch``), read before ``session/load`` hoists servers out
        # of the batch and carried across a handle swap, so shutdown removes them.
        self.kas_launch_snapshots: list[str] = []
        # The upgrade notice when that batch's prompt is dropped by the KAS this
        # process runs (``harness.kas.kas_agent_prompt_issue``); "" otherwise.
        self.kas_agent_prompt_notice: str = ""
        # JSON-RPC request id -> {"once","always","reject"} optionId map, so
        # approve_tool / reject_tool echo the exact ids the agent advertised
        # (kiro "allow_once"/"allow_always"; claude-agent-acp "allow"/"reject").
        self._permission_options: dict[str | int, dict[str, str]] = {}
        # Request id -> the permission event built for it, so approve_tool can
        # put the request through the security floor (``permission_floor``)
        # whichever consumer answers it.
        self._permission_gate_events: dict[str | int, AcpEvent] = {}
        # req_ids of in-flight _wait_for_response calls (send_command /
        # set_config_option / compact). The prompt dispatch loop shares this
        # session's queue, so when it dequeues one of these responses it uses
        # this set to hand it back promptly (vs. dropping / holding to turn end).
        self._awaited_responses: set[int] = set()
        self.last_prompt_stats = AcpPromptStats()
        # State tracking (populated from session/new response via store_session_config)
        self._model: str = ""
        self.active_agent: str = ""
        # The vault identity fingerprint read as THIS session's ``session/new``
        # or ``session/load`` went out (``kas_model_catalog.request_vault_identity``);
        # ``""`` on a handle whose request was never stamped, which records
        # nothing. The KAS catalog recorder keeps it with the response's
        # ``model`` select: the engine answers for the account in the vault when
        # it was asked, and the picker serves the rows only to that account.
        self.request_vault_identity: str = ""
        # Model id kiro-cli RESOLVED the session to (from currentModelId in the
        # session/new|load response), kept separate from _model (the user-picked
        # alias) so it feeds ONLY the context-window backfill — never slot.model
        # (mirrors AcpClient._resolved_model_id; avoids the profile-id
        # pinning trap where a resolved profile id poisons slot.model).
        self._resolved_model_id: str = ""
        # True after set_mode switched a live session's agent, until the model
        # kiro-cli reported for that activation has been judged against the
        # served list (see _served_default_after_set_mode).
        self._mode_model_unjudged: bool = False
        # The model a non-strict config-option push was refused on, or the
        # startup pin the provider factory withheld because the account does not
        # serve it, or ``""`` (mirrors AcpClient.model_pin_refused). Either way
        # the session stays on the backend default without raising, so this is
        # the only trace of it.
        self.model_pin_refused: str = ""
        # The bare model a pair pin landed as when its effort was refused
        # (mirrors AcpClient.model_pin_partial).
        self.model_pin_partial: str = ""
        self._config_options: list[dict[str, Any]] = []
        self._available_models: list[dict[str, str]] = []
        # Set by ``_feed_advertised_model_cache`` when the capture changed the
        # cross-session advertised-model cache, so the async init caller can
        # offload the disk persist off the event loop. Mirrors
        # ``AcpClient._advertised_models_changed`` on the direct-spawn path.
        self._advertised_models_changed: bool = False
        # Read by KAS only: the listed id a session born on an unlisted ``auto`` moves to,
        # or "". One answer read from the ``model`` select, never the list itself:
        # nobody has shown that list to be the account's complete served set, so
        # it must not become the entitlement list the picker and the explicit-pick
        # guard narrow by.
        self._kas_auto_fallback: str = ""
        # Read-path revalidation bookkeeping (see maybe_refresh_available_models).
        # The session-init snapshot is one unconfirmed answer captured at one
        # instant, and the read path (the dashboard picker filter) has no
        # explicit-pick refusal to trigger the refresh-before-refuse path — so it
        # must decide for itself whether a snapshot that would NARROW the catalog
        # is trustworthy. These three fields are the staleness signals it reads;
        # each is a monotonic timestamp or a confirmation flag, never entitlement
        # evidence (the keep/drop verdict stays with ``catalog_row_would_drop``).
        # 0.0 = never captured (no session/new stored a list yet).
        self._available_models_captured_at: float = 0.0
        # True once a probe (refresh_available_models) has confirmed the snapshot
        # against the live backend — the strongest "trust it" signal.
        self._available_models_probe_confirmed: bool = False
        # Monotonic time the read-path heuristic last kicked a probe for THIS
        # session, so a hot dashboard poll cannot re-probe every TTL expiry
        # forever once a legitimately-narrow snapshot has been confirmed.
        self._available_models_read_probe_at: float = 0.0
        # Single in-flight read-path refresh task per handle. The read path
        # shields it, so a deadline miss raises EntitlementRevalidating (the
        # endpoint answers 503 and the client re-polls) WITHOUT cancelling the
        # probe — the task keeps running to completion so its throwaway session
        # is cleaned up and the next read serves its result.
        self._read_refresh_task: asyncio.Task[list[dict[str, str]]] | None = None
        # Last KAS mode id seen on a current_mode_update, so a re-assert of the
        # already-current mode does not surface a spurious agent-switch echo
        # (kiro-cli only emits on a real _kiro.dev/agent/switched). None = unseen.
        self._last_kas_mode_id: str | None = None
        # (turn ordinal, KAS row id) of the user row KAS stored for this handle's
        # latest prompt; what :meth:`discard_unanswered_prompt` rewinds to.
        self._kas_prompt_row: tuple[int, str] | None = None
        # KAS sub-agent roster keyed by agentSubtaskId. Each entry is shaped for
        # EVENT_SUBAGENT_LIST consumption by _native_subagent_sync in chat_runner.
        self._kas_subagent_roster: dict[str, dict[str, Any]] = {}

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def prompt_or_tool_seen(self) -> bool:
        """True once this session observed a text chunk or a tool call.

        The registration-throttle death classification reads it (here and in
        ``AcpSessionProvider._translate_dead``): the transient verdict may only
        license replaying a session that provably produced no output and ran no
        tool, so the window closes at the first observed event and never
        reopens.
        """
        return self._prompt_or_tool_seen

    @property
    def prompt_outstanding_on_stall(self) -> bool:
        """Whether this session's death may have left its prompt delivered.

        True when the runtime died of a stdin stall with the child still alive
        while this session's turn was running with its prompt frame written.
        That frame is in the pipe the child stopped reading, so the child may
        still read it and act: the death is an ambiguous delivery (see
        ``AcpProcessDied``) for every such session, not only for the one whose
        write tripped the bound. The turn state is the runtime's snapshot at the
        death, so the answer holds after this turn's own teardown has run.
        """
        at_stall = getattr(self._runtime, "turn_active_at_stall", None)
        return (
            bool(self._prompt_written) and callable(at_stall) and at_stall(self._session_id) is True
        )

    @property
    def is_turn_active(self) -> bool:
        # Factor _cancelled (parity with AcpClient.has_active_turn) so a second
        # cancel() is a no-op early-return instead of re-sending session/cancel.
        # Also require the runtime alive (parity: AcpClient checks _is_process_alive)
        # so a turn on a dead runtime reads inactive -> AcpProvider.cancel() returns
        # "no_turn" instead of firing cancel_session on a corpse.
        return (not self._turn_done.is_set()) and (not self._cancelled) and self._runtime.is_alive()

    @property
    def has_unfinished_turn(self) -> bool:
        """True if the native turn has not reached its done boundary and the
        runtime is alive — INDEPENDENT of ``_cancelled`` (unlike
        :attr:`is_turn_active`).

        A turn that has been ``cancel()``'d but whose turn-done ack has not yet
        arrived still holds the native turn open; the shutdown drain must still
        wait on it before the runtime is killed, or kiro-cli's session lock is
        left held (the empty-response-after-restart bug).
        """
        return (not self._turn_done.is_set()) and self._runtime.is_alive()

    @property
    def parked_since(self) -> float | None:
        """Monotonic timestamp the current park began, or None if not parked.

        Exposed so an observer can latch on a park's IDENTITY rather than its
        duration: a park that outlives the observer's tick would otherwise be
        re-reported on every pass.
        """
        return self._parked_since

    @property
    def awaiting_permission(self) -> bool:
        """True while a permission event has been yielded and not yet answered.

        A turn parked here is waiting for a HUMAN, which is not a stall: that wait
        is already bounded by ``agent.tool_approval_timeout_secs``. An external
        observer must exclude it — otherwise every approval prompt reads as a
        stalled turn, and two components end up racing to end the same wait on
        different budgets.
        """
        return self._awaiting_permission

    # Monotonic stamp of the last steer handed to the backend, 0.0 when this
    # session has never been steered. Read by the dashboard's keepalive route to
    # decide whether a sleeping `wait` should return early: a steer can only be
    # injected at a model-inference boundary, and an in-flight tool call is the
    # absence of one, so a sleep that outlasts the steer would hold the user's
    # correction in the backend's queue until it elapses.
    #
    # Deliberately monotonic, not wall clock: it is only ever compared against
    # another monotonic stamp taken in the same process (the sleep's start), and
    # mixing the two clocks is how a suspend-resume silently reorders them.
    _last_steer_monotonic: float = 0.0

    # Set once kiro-cli refuses ``_session/steer/clear`` (a build without it), so
    # the session stops offering a pending steer back. See ``clear_pending_steers``.
    _steer_clear_refused: bool = False

    @property
    def last_steer_monotonic(self) -> float:
        """Monotonic time of the last steer written to the backend (0.0 if none)."""
        return self._last_steer_monotonic

    @property
    def supports_steer(self) -> bool:
        """True when this session's host takes a user's mid-turn message.

        Membership in ``ACP_BACKENDS_STEER`` (harness-parity H6) or in
        ``ACP_BACKENDS_STEERING_REQUEST``, read from the runtime's own backend id.
        The first is kiro-cli's ``_session/steer``, the same answer, from the same
        table, that ``AcpClient.supports_steer`` gives; the second is codex-acp's
        ``_session/steering``, which only this handle speaks, and only when the
        process's handshake advertised it (``_meta.steering.supported: true``). A
        capability is granted by opt-in membership, so a host the runtime learns to
        drive does not inherit an extension it never demonstrated: answering True
        for one would advertise the steer affordance and then meet the user's
        mid-turn correction with ``-32601``.
        """
        backend = self._runtime.acp_backend
        if backend in ACP_BACKENDS_STEER:
            return True
        return backend in ACP_BACKENDS_STEERING_REQUEST and steering_advertised(
            self.initialize_result
        )

    @property
    def supports_steer_retract(self) -> bool:
        """True when a steer this session has not read yet can be taken back.

        Membership in ``ACP_BACKENDS_STEER_RETRACT``, read from the runtime's own
        backend id, and False once the host refused the request
        (:meth:`clear_pending_steers`), so a build without it is not offered again.
        """
        return (
            self._runtime.acp_backend in ACP_BACKENDS_STEER_RETRACT
            and not self._steer_clear_refused
        )

    @property
    def supports_refusal_steer(self) -> bool:
        """True when a deny notice steered into the refused turn reaches the model.

        Narrower than :attr:`supports_steer`: only ``ACP_BACKENDS_STEER``. codex
        takes a user's steer, but its approval answer cancels the turn and the
        injected text goes with it, so a deny notice there keeps the recovery
        continuation instead.
        """
        return self._runtime.acp_backend in ACP_BACKENDS_STEER

    @property
    def steer_needs_loss_recovery(self) -> bool:
        """True when an accepted steer can still be dropped with its turn.

        codex (``ACP_BACKENDS_STEERING_REQUEST``) drops injected text when a later
        approval in the turn is denied or the turn is cancelled, so an accepted
        steer is reported consumed only at a clean terminal and the caller must
        keep and requeue it otherwise. Only the dashboard composer, whose pending
        entry the turn's teardown requeues, steers such a session; the
        provider-wrapper surfaces (messaging channels, Side Chat, ``spawn_steer``)
        read this and queue instead.
        """
        return self._runtime.acp_backend in ACP_BACKENDS_STEERING_REQUEST

    # ── State tracking ──

    @property
    def model(self) -> str:
        """Current model name for this session."""
        return self._model

    @property
    def served_model(self) -> str:
        """Backend-resolved model id serving this session (``""`` until known).

        Prefers the explicit ``set_model`` assignment (``_model``), falling
        back to the ``session/new|load`` response's ``currentModelId``
        (``_resolved_model_id``) so a session running on the backend-selected
        DEFAULT is still readable — ``_model`` stays ``""`` on that path.
        Both sources are backend-confirmed; the requested alias is never
        reported here. May be a profile-form id, which is a valid wire id.
        """
        return self._model or self._resolved_model_id

    @property
    def agent_version(self) -> str:
        """The version the shared process RUNS (``""`` until its handshake).

        Delegates to the runtime because the handshake is per process, not per
        session: every handle on one runtime reports the same value.
        """
        return self._runtime.agent_version

    @property
    def initialize_result(self) -> dict:
        """The shared process's ``initialize`` result (``{}`` until its handshake)."""
        init = getattr(self._runtime, "initialize_result", None)
        return init if isinstance(init, dict) else {}

    @property
    def config_options(self) -> list[dict[str, Any]]:
        """ACP-reported configOptions (effort, model, mode selectors)."""
        return self._config_options

    @property
    def available_models(self) -> list[dict[str, str]]:
        """Models advertised by the backend at session init."""
        return list(self._available_models)

    # ── In-flight tool state (delegated to the shared tracker) ──
    # These read-only views keep the call sites that read the judged tool
    # unchanged while the hand-off rule lives in ``InFlightToolTracker``. The
    # dispatch / terminal / prompt-start writes go through the tracker directly
    # (see ``_dispatch_events`` and ``prompt``).

    @property
    def _inflight_tool(self) -> ToolCallState | None:
        """The :class:`ToolCallState` a stall verdict is read against."""
        return self._tool_tracker.current

    @_inflight_tool.setter
    def _inflight_tool(self, value: ToolCallState | None) -> None:
        # A direct set stages one call (the single-slot shape the watchdog tests
        # use to force the oracle path): ``None`` clears, otherwise it becomes the
        # judged call under the empty-id slot. The normal dispatch path writes
        # through ``_tool_tracker.dispatch`` with the real toolCallId.
        if value is None:
            self._tool_tracker.clear()
        else:
            self._tool_tracker.dispatch("", value)

    @property
    def _active_tool_calls(
        self,
    ) -> dict[str, tuple[ToolCallState, "InteractiveClassification | None"]]:
        """Live ``{toolCallId: (state, interactive)}`` map, backed by the shared
        tracker. Read-only by convention — the hand-off goes through the tracker
        (see ``_dispatch_events`` and ``prompt``)."""
        return self._tool_tracker.active_calls  # type: ignore[return-value]

    @property
    def _inflight_interactive(self) -> "InteractiveClassification | None":
        """Pre-dispatch interactive classification of the in-flight SHELL tool
        (``None`` when no shell tool is in flight). Carried as the tracker's aux
        payload. Read by the tool branch's window policy and the post-stall
        classifier; never by the oracle."""
        aux = self._tool_tracker.current_aux
        return aux  # type: ignore[return-value]

    @property
    def _inflight_tool_call_id(self) -> str:
        """toolCallId of the in-flight tool ("" when none)."""
        return self._tool_tracker.current_id

    @property
    def status_rejections(self) -> dict[str, int]:
        """Per-reason count of ``kirocrew/status`` frames rejected this turn."""
        return dict(self._status_rejected)

    @property
    def inflight_interactive(self) -> InteractiveClassification | None:
        """The in-flight shell tool's interactive classification, if any."""
        return self._inflight_interactive

    @property
    def native_child_sessions(self) -> frozenset[str]:
        """Harness-native child session ids observed this turn.

        The count at the parent recovery boundary (RFC §14.8): these children
        have identity (a session id) and attributable tool events, but no
        cancel or resume of their own — ``session/cancel`` on THIS session is
        the only lever, and it takes all of them. Display-only otherwise; a
        scheduler must never read this as N pausable tasks.

        Fed by three seams, all on the execution layer and never on model
        text: child-routed ``session/update`` frames (``_handle_update``), the
        ``_kiro.dev/session/update`` child stream (``_dispatch_events``), and
        the roster notifications — kiro-cli ``subagent_list`` when this handle
        is the roster's sole owner, KAS ``agentSubtaskId`` / pipeline stages
        (``_handle_kas_subagent``). A ``spawn_run`` child of this session is
        NOT here: it is a Kiro Crew task row with its own handle, and native
        grandchildren under it are counted on THAT handle (parity row H16).
        """
        return frozenset(self._native_child_sids)

    @property
    def native_child_overflow(self) -> int:
        """Native child ids seen past :data:`NATIVE_CHILD_ROSTER_CAP` this turn
        — counted, never stored, and deliberately never de-duplicated:
        de-duplicating an id means remembering it, which is the one thing the
        cap exists to refuse."""
        return self._native_child_overflow


own_methods(AcpSessionHandle, _MEMBER_OWNERS)
