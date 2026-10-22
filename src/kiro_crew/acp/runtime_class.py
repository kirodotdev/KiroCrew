"""``class AcpRuntime``: the runtime's constructor, state, properties and constants.

The class body binds each member an owner module defines (``spawn = _rt_entry.spawn``) and
keeps the constructor, the properties and the class constants. ``_own_methods`` names each
bound function after its class attribute. ``kiro_crew.acp.runtime`` re-exports the class,
which keeps that module as its ``__module__``: the facade is the import path callers, pickles
and reprs name.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.acp import kas_secret_storage
from kiro_crew.acp import runtime_frame_accounting as _rt_frames
from kiro_crew.acp import runtime_host_requests as _rt_host
from kiro_crew.acp import runtime_liveness as _rt_liveness
from kiro_crew.acp import runtime_mode_activation as _rt_mode
from kiro_crew.acp import runtime_reader_loop as _rt_reader
from kiro_crew.acp import runtime_session_create as _rt_create
from kiro_crew.acp import runtime_session_load as _rt_load
from kiro_crew.acp import runtime_session_registry as _rt_registry
from kiro_crew.acp import runtime_session_setup as _rt_setup
from kiro_crew.acp import runtime_spawn_entry as _rt_entry
from kiro_crew.acp import runtime_spawn_launch as _rt_launch
from kiro_crew.acp import runtime_tree_kill as _rt_tree
from kiro_crew.acp import runtime_wire as _rt_wire
from kiro_crew.acp.client import CLIENT_NAME
from kiro_crew.acp.harness import HarnessAdapter, harness_for
from kiro_crew.acp.runtime_liveness import _DEFAULT_MAX_AGE_SECS, _DEFAULT_MAX_RSS_MB
from kiro_crew.acp.runtime_process_tree import ChildRecord
from kiro_crew.acp.runtime_start import _INIT_NOTIFICATION_BUFFER_LIMIT, StartCollector
from kiro_crew.acp.runtime_wire import _PendingRequests
from kiro_crew.acp.types import ACP_BACKEND_KIRO, JsonRpcMessage, backends_retired_by_host_logout
from kiro_crew.agent_sdk.tool_search import ToolSearchSettings
from kiro_crew.validation import MODEL_ID_RE

if TYPE_CHECKING:
    from kiro_crew.acp.skill_projection import NativeSkillProjection

#: The owner modules whose functions ``AcpRuntime`` binds as its members.
_MEMBER_OWNERS = frozenset(
    {
        "kiro_crew.acp.runtime_tree_kill",
        "kiro_crew.acp.runtime_spawn_launch",
        "kiro_crew.acp.runtime_spawn_entry",
        "kiro_crew.acp.runtime_session_setup",
        "kiro_crew.acp.runtime_session_create",
        "kiro_crew.acp.runtime_session_load",
        "kiro_crew.acp.runtime_session_registry",
        "kiro_crew.acp.runtime_mode_activation",
        "kiro_crew.acp.runtime_wire",
        "kiro_crew.acp.runtime_liveness",
        "kiro_crew.acp.runtime_host_requests",
        "kiro_crew.acp.runtime_frame_accounting",
        "kiro_crew.acp.runtime_reader_loop",
    }
)


def _own_methods(cls: type) -> type:
    """Make each member *cls* binds from an owner module read as a method of *cls*.

    A bound function annotates ``self`` so mypy checks its body as a method body;
    this drops that annotation from the runtime object and names the function after
    the class attribute (``AcpRuntime._spawn_admitted``), so its ``inspect.signature``
    and ``__qualname__`` are what the one-module class had, and so are the reprs of
    the coroutines and tasks it starts. Only ``__module__`` moves: it names the owner
    module that holds the body.
    """
    for name, member in vars(cls).items():
        function = getattr(member, "__func__", member)
        if getattr(function, "__module__", None) in _MEMBER_OWNERS:
            function.__annotations__.pop("self", None)
            function.__qualname__ = f"{cls.__qualname__}.{name}"
    return cls


@_own_methods
class AcpRuntime:
    """Owns one kiro-cli acp subprocess with single-reader demux.

    The _reader_task is the ONLY coroutine that reads from stdout.
    It routes frames by:
      - 'id' field in _pending_requests → resolve Future (for send_and_await)
      - 'id' field in _routed_requests → put in session queue (for prompt responses)
      - params.sessionId → _session_queues[sessionId].put(msg)
      - no sessionId → broadcast to all session queues
    """

    # The facade is the class's import path, so pickles and reprs keep naming it.
    __module__ = "kiro_crew.acp.runtime"

    # Fields a bound member assigns first, outside ``__init__``: declared for type
    # checkers only, so the class carries no annotations at run time and a runtime
    # built with ``__new__`` still reads them through the ``getattr`` defaults their
    # readers pass.
    if TYPE_CHECKING:
        _native_skill_projection: NativeSkillProjection | None
        _skill_projection_generation: int
        _spawn_skill_projection: NativeSkillProjection | None
        _skill_projection_issued: int
        _skill_projection_lock_obj: asyncio.Lock
        _skill_projection_unadopted: int
        _stdin_lock: asyncio.Lock

    # The process tree and its teardown: ``kiro_crew.acp.runtime_tree_kill``.
    _discard_bound_workspace = _rt_tree._discard_bound_workspace
    _root_identity = _rt_tree._root_identity
    _root_identity_holds = _rt_tree._root_identity_holds
    refresh_process_tree = _rt_tree.refresh_process_tree
    _snapshot_descendants = _rt_tree._snapshot_descendants
    _signal_tree = _rt_tree._signal_tree
    kill = _rt_tree.kill
    _kill_inner = _rt_tree._kill_inner
    # Launching the child process and its handshake: ``kiro_crew.acp.runtime_spawn_launch``.
    _discard_sandbox_cleanup = _rt_launch._discard_sandbox_cleanup
    _to_thread_guarding_sandbox = _rt_launch._to_thread_guarding_sandbox
    _resolve_spawn_plan = _rt_launch._resolve_spawn_plan
    _initialize_handshake = _rt_launch._initialize_handshake
    _spawn_admitted = _rt_launch._spawn_admitted
    _failed_start_cleanup = _rt_launch._failed_start_cleanup
    _record_tree_before_failed_start_kill = _rt_launch._record_tree_before_failed_start_kill
    _kill_failed_start_escapees = _rt_launch._kill_failed_start_escapees
    _handshake_client_capabilities = _rt_launch._handshake_client_capabilities
    _projected_spawn_spec = _rt_launch._projected_spawn_spec
    _source_agent = staticmethod(_rt_launch._source_agent)
    # The spawn entry point: ``kiro_crew.acp.runtime_spawn_entry``.
    spawn = _rt_entry.spawn
    _spawn_admitted_rederiving_once = _rt_entry._spawn_admitted_rederiving_once
    _revive_after_failed_start = _rt_entry._revive_after_failed_start
    # What a session start is built from: ``kiro_crew.acp.runtime_session_setup``.
    _session_work_dir = _rt_setup._session_work_dir
    _refuse_if_loader_unreachable = _rt_setup._refuse_if_loader_unreachable
    _kas_custom_agents = _rt_setup._kas_custom_agents
    _mount_member_panel = _rt_setup._mount_member_panel
    _session_start_budget = _rt_setup._session_start_budget
    _mirrored_session_mcp = _rt_setup._mirrored_session_mcp
    _mirrored_spec_check_needed = _rt_setup._mirrored_spec_check_needed
    _member_projection_needs_confirmation = _rt_setup._member_projection_needs_confirmation
    _require_unchanged_mirrored_spec = _rt_setup._require_unchanged_mirrored_spec
    _confirm_member_projection = _rt_setup._confirm_member_projection
    _own_stub_session = _rt_setup._own_stub_session
    _unpooled_control_planes = _rt_setup._unpooled_control_planes
    _copy_native_launch_sources = _rt_setup._copy_native_launch_sources
    _guard_unresolved_mcp_refs = _rt_setup._guard_unresolved_mcp_refs
    _start_mount_check = _rt_setup._start_mount_check
    # ``session/new`` end to end: ``kiro_crew.acp.runtime_session_create``.
    create_session = _rt_create.create_session
    _collect_late_start = _rt_create._collect_late_start
    _teardown_late_session = _rt_create._teardown_late_session
    start_collectors = _rt_create.start_collectors
    _finish_create_session = _rt_create._finish_create_session
    probe_advertised_models = _rt_create.probe_advertised_models
    # ``session/load`` end to end: ``kiro_crew.acp.runtime_session_load``.
    load_session = _rt_load.load_session
    _wait_managed_mcp = _rt_load._wait_managed_mcp
    # The sessions held and how each leaves: ``kiro_crew.acp.runtime_session_registry``.
    unregister_session = _rt_registry.unregister_session
    mark_turn_active = _rt_registry.mark_turn_active
    terminate_session = _rt_registry.terminate_session
    _session_teardown_method = _rt_registry._session_teardown_method
    _mcp_init_progress = _rt_registry._mcp_init_progress
    _session_start_stalled = _rt_registry._session_start_stalled
    _stage_init_frame = _rt_registry._stage_init_frame
    _finish_session_init = _rt_registry._finish_session_init
    # Putting a session on its agent: ``kiro_crew.acp.runtime_mode_activation``.
    _skill_projection_lock = _rt_mode._skill_projection_lock
    _issue_skill_projection_generation = _rt_mode._issue_skill_projection_generation
    _adopted_skill_projection_generation = _rt_mode._adopted_skill_projection_generation
    _note_unadopted_skill_projection = _rt_mode._note_unadopted_skill_projection
    _unadopted_skill_projection_generation = _rt_mode._unadopted_skill_projection_generation
    _adopt_skill_projection = _rt_mode._adopt_skill_projection
    _activates_agent_by_mode = _rt_mode._activates_agent_by_mode
    _mode_available = staticmethod(_rt_mode._mode_available)
    _verify_spawn_agent_active = _rt_mode._verify_spawn_agent_active
    _spawn_agent_not_loaded_reason = _rt_mode._spawn_agent_not_loaded_reason
    _resolve_start_alias = _rt_mode._resolve_start_alias
    _superseding_alias = _rt_mode._superseding_alias
    _refuse_if_view_unverified = _rt_mode._refuse_if_view_unverified
    _switched_model_record = _rt_mode._switched_model_record
    _note_switched_model = _rt_mode._note_switched_model
    take_switched_model = _rt_mode.take_switched_model
    _served_default_after_mode = _rt_mode._served_default_after_mode
    _refuse_if_view_superseded = _rt_mode._refuse_if_view_superseded
    _activate_mode_bracketed = _rt_mode._activate_mode_bracketed
    # JSON-RPC over the child's stdin: ``kiro_crew.acp.runtime_wire``.
    send_request = _rt_wire.send_request
    send_notification = _rt_wire.send_notification
    _refuse_write_if_dead = _rt_wire._refuse_write_if_dead
    _stdin_write_lock = _rt_wire._stdin_write_lock
    _write_response_bounded = _rt_wire._write_response_bounded
    _write_request_bounded = _rt_wire._write_request_bounded
    send_request_for_answer = _rt_wire.send_request_for_answer
    forget_request = _rt_wire.forget_request
    send_response = _rt_wire.send_response
    send_error = _rt_wire.send_error
    _one_at_a_time_ahead = _rt_wire._one_at_a_time_ahead
    _send_and_await = _rt_wire._send_and_await
    # Alive, busy, dead or due to recycle: ``kiro_crew.acp.runtime_liveness``.
    is_alive = _rt_liveness.is_alive
    stdin_reader_may_live = _rt_liveness.stdin_reader_may_live
    turn_active_at_stall = _rt_liveness.turn_active_at_stall
    death_summary = _rt_liveness.death_summary
    _stale_by_age = _rt_liveness._stale_by_age
    _is_stale = _rt_liveness._is_stale
    has_active_sessions = _rt_liveness.has_active_sessions
    has_active_or_initializing_sessions = _rt_liveness.has_active_or_initializing_sessions
    saw_not_logged_in = _rt_liveness.saw_not_logged_in
    settle_stderr = _rt_liveness.settle_stderr
    saw_sandbox_init_failure = _rt_liveness.saw_sandbox_init_failure
    saw_delegate_launch_failure = _rt_liveness.saw_delegate_launch_failure
    redacted_stderr_tail = _rt_liveness.redacted_stderr_tail
    _exit_watch_loop = _rt_liveness._exit_watch_loop
    _reader_has_buffered_input = _rt_liveness._reader_has_buffered_input
    _exit_reason = _rt_liveness._exit_reason
    _returncode_label = _rt_liveness._returncode_label
    _compose_death_summary = _rt_liveness._compose_death_summary
    _note_reaped_after_kill = _rt_liveness._note_reaped_after_kill
    _note_reaped = _rt_liveness._note_reaped
    _mark_dead = _rt_liveness._mark_dead
    _reap_after_cancelled_tracking = _rt_liveness._reap_after_cancelled_tracking
    _retire_tracking_after_exit = _rt_liveness._retire_tracking_after_exit
    _drain_stderr = _rt_liveness._drain_stderr
    # Requests no session turn answers: ``kiro_crew.acp.runtime_host_requests``.
    _wait_for_answer_capacity = _rt_host._wait_for_answer_capacity
    _spawn_answer_task = _rt_host._spawn_answer_task
    _answer_unroutable_permission = _rt_host._answer_unroutable_permission
    _audit_denied_off_loop = _rt_host._audit_denied_off_loop
    _answer_get_access_token = _rt_host._answer_get_access_token
    _answer_host_request = _rt_host._answer_host_request
    mcp_sign_in_holds = _rt_host.mcp_sign_in_holds
    begin_mcp_sign_in = _rt_host.begin_mcp_sign_in
    _run_mcp_sign_in = _rt_host._run_mcp_sign_in
    begin_mcp_reattempt = _rt_host.begin_mcp_reattempt
    _run_mcp_reattempt = _rt_host._run_mcp_reattempt
    _answer_open_external_url = _rt_host._answer_open_external_url
    _answer_secret_request = _rt_host._answer_secret_request
    _answer_ownerless_request = _rt_host._answer_ownerless_request
    # What the reader drops, counted: ``kiro_crew.acp.runtime_frame_accounting``.
    _snapshot_subagent_sessions = _rt_frames._snapshot_subagent_sessions
    _note_roster_overflow = _rt_frames._note_roster_overflow
    _flush_roster_overflow = _rt_frames._flush_roster_overflow
    _end_roster_overflow_episode = _rt_frames._end_roster_overflow_episode
    _note_dropped_frame = _rt_frames._note_dropped_frame
    _flush_dropped_frames = _rt_frames._flush_dropped_frames
    # The single stdout reader: ``kiro_crew.acp.runtime_reader_loop``.
    _reader_loop = _rt_reader._reader_loop
    _notification_aliases = _rt_reader._notification_aliases

    #: See ``__init__``. Set on the class too, so a runtime built without
    #: ``__init__`` (a test double, a restored instance) reads as having none.
    _launch_agent = ""

    def __init__(
        self,
        work_dir: str | Path | None = None,
        agent: str = CLIENT_NAME,
        sandbox_mode: str = "auto",
        extra_env: dict[str, str] | None = None,
        mcp_gateway_overlay: str | Path | None = None,
        mcp_gateway_socket: str | Path | None = None,
        max_age_secs: float = _DEFAULT_MAX_AGE_SECS,
        max_rss_mb: float = _DEFAULT_MAX_RSS_MB,
        model: str | None = None,
        expect_mcp_reports: bool = True,
        acp_backend: str = ACP_BACKEND_KIRO,
        crew_agent: str = "",
        member_context: bool = False,
        memory_mode: str = "persistent",
        tool_search: ToolSearchSettings | None = None,
        shared_scratch: Path | None = None,
        launch_agent: str = "",
    ):
        if work_dir:
            self._work_dir = Path(work_dir)
        else:
            # config.paths is a stdlib-only leaf: importing it here can't
            # re-enter the config.loader -> providers.acp -> acp.client cycle.
            from kiro_crew.config.paths import config_dir

            self._work_dir = config_dir() / "workspace"
        self._agent = agent
        # The name kiro-cli is asked for in place of ``agent`` -- on ``--agent`` and
        # in ``session/set_mode`` -- when they differ: an app's launch copy
        # (``apps.agent_launch``). Wire-only, like KAS's ``kas_wire_agent_id``:
        # ``self._agent`` stays the identity every gate, profile, audit row and
        # hook reads. Empty for every other runtime.
        self._launch_agent = launch_agent
        # Canonical Kiro Crew agent identity (a cfg.agents key) resolved by the
        # surface that created this runtime — a DIFFERENT namespace from
        # ``agent`` (the kiro template the process spawns with). Default for
        # sessions created on this runtime; a warm-pool rekey overwrites it so
        # later sessions inherit the claiming crew, not the pool's spawn state.
        self._crew_agent = crew_agent
        self._acp_backend = acp_backend
        # The operator's MCP Tool Search choice, for hosts that take it over the
        # wire (``client_meta_settings``). None leaves the handshake as the harness
        # declares it. ``_tool_search_wire`` is what was actually sent, kept so a
        # later session on this process can be judged against the process-wide
        # setting it inherits (see create_session).
        self._tool_search = tool_search
        self._tool_search_wire: dict[str, Any] = {}
        # Resolved on FIRST USE, never here: ``ACP_BACKENDS_KNOWN`` admits
        # backends the shared-process runtime has no harness for, and provider
        # safety constructs a runtime for every one of them to prove the reader
        # loop survives a recorder fault. Resolving in __init__ would make those
        # constructions raise, so the failure is deferred to the first seam that
        # actually needs a host's answer -- a runtime that never spawns and never
        # starts a session never needs one.
        self._harness_resolved: HarnessAdapter | None = None
        # Whether THIS process was spawned with Crew as the engine's auth owner
        # (relay started without ``--auth-method cli`` because the Crew vault
        # held an identity at spawn). Decided once in _resolve_spawn_plan and
        # read by the reader loop: a credential callback is answered from the
        # vault only on a process that was spawned expecting it.
        self._kas_host_auth = False
        # Answers this process's ``_kiro/secret/*`` requests when its handshake
        # declared ``secretStorage`` (``agent.kas_persist_mcp_sign_in``); None
        # otherwise, and those requests then get -32601. Set at each handshake.
        self._mcp_secret_server: kas_secret_storage.SecretStorageServer | None = None
        # First answered credential callback per runtime is logged at INFO as a
        # positive "the engine is drawing its credential from Crew" signal; later
        # ones (the engine refreshes ahead of expiry) drop to DEBUG.
        self._kas_host_auth_logged = False
        # The one MCP sign-in in flight on this process, as (session_id,
        # server_name). The engine's consent-URL request names neither, so at
        # most one sign-in runs at a time and its URL goes to the session that
        # started it -- see begin_mcp_sign_in.
        self._mcp_sign_in: tuple[str, str] | None = None
        # MCP re-attempts in flight on this process, as (session_id,
        # server_name): a reset without startOAuth for a server that failed
        # for want of a credential -- see begin_mcp_reattempt.
        self._mcp_reattempts: set[tuple[str, str]] = set()
        if model is not None:
            if not MODEL_ID_RE.match(model):
                raise ValueError(
                    f"Invalid model identifier: {model!r} — must match "
                    f"^[a-zA-Z0-9][a-zA-Z0-9._-]{{0,127}}$"
                )
        self._model = model
        self._sandbox_mode = sandbox_mode
        self._member_context = member_context
        if memory_mode not in {"persistent", "incognito", "temporary"}:
            raise ValueError("Invalid session memory mode")
        self.recording_allowed = memory_mode == "persistent"
        self._native_launch_sources: dict[str, str] = {}
        # Set only by the non-member launch record below: the projected view
        # alias the record was taken from, and the view's declared resources,
        # whose on-disk total bounds every read of the record.
        self._native_launch_view_alias: str | None = None
        self._native_launch_resources: list[Any] | None = None
        self._extra_env = extra_env or {}
        self._mcp_gateway_overlay = str(mcp_gateway_overlay) if mcp_gateway_overlay else None
        self._mcp_gateway_socket = str(mcp_gateway_socket) if mcp_gateway_socket else None
        # Whether sessions on this runtime should hold drain_init() open for
        # slow MCP servers (the no-report ceiling). A runtime whose agent is
        # KNOWN to have zero MCP servers — the kirocrew-lite background runtime,
        # whose config Kiro Crew itself writes with an empty mcpServers map —
        # opts out so hot one-liner paths (chat titles, suggestions, STT
        # endpointing) don't pay a full ceiling wait that can never be armed.
        self._expect_mcp_reports = expect_mcp_reports
        self._sandbox_cleanup: str | None = None
        self._bound_workspace_fd: int | None = None
        self._spawn_work_dir = str(self._work_dir)
        # The directory the kiro-cli PROCESS starts in when it runs an app's launch
        # copy (``apps.agent_launch.launch_process_cwd``), set at spawn; empty for
        # every other runtime, whose process starts in ``_work_dir``.
        self._launch_process_cwd = ""
        # Its spellings, resolved once at spawn off the loop (``launch_cwd_roots``).
        self._launch_cwd_roots: tuple[str, ...] = ()
        # The launch copy as read by its path at spawn (``apps.agent_launch.read_launch_copy``):
        # the spec Crew's own session-start reads judge for it, never a name lookup.
        self._launch_copy_spec: dict[str, Any] | None = None
        # sessionId -> the cwd its ``session/new`` / ``session/load`` named, so the
        # paths kiro-cli reports against ``_launch_process_cwd`` map to it.
        self._session_cwds: dict[str, str] = {}
        # The session tree's work directory when this runtime is not the tree's
        # first process (a companion runtime spawned for a parent's subagents,
        # or the successor of a recycled ``_bg`` runtime). Re-validated at spawn
        # (``agent_scratch.shared_scratch_window``); ``None`` means this process
        # starts a tree and its own directory is the work directory. Once live,
        # the process adds itself to the tree's owner marker beside every other
        # live user (``agent_scratch.adopt_owner``), so the sweep keeps the tree
        # while any of them runs.
        self._shared_scratch: Path | None = Path(shared_scratch) if shared_scratch else None
        self._scratch_dir: Path | None = None
        # What the pre-spawn freshness check verified, for the post-handshake half of
        # the bracket. ``None`` until a spawn takes it, and ``None`` for every agent
        # that mirrors no other spec.
        self._derived_spec_snapshot: Any = None

        # Recycling thresholds — see _is_stale(). Long-lived multiplexed
        # runtimes (e.g. the kirocrew-lite background runtime) have no
        # per-turn compaction, so age/RSS are the only signals available to
        # bound unbounded growth.
        #
        # The operator's values, held as the INPUT to the host's own policy: a
        # spawn passes them through ``harness.reclaim_policy`` so a host known to
        # leak faster can narrow them with no branch here, and a host that does
        # not narrow leaves the operator's configuration as the whole answer.
        # Applied at spawn rather than here because a runtime is constructible for
        # a backend the shared-process runtime has no harness for, and because
        # neither threshold means anything before a process exists.
        self._max_age_secs = max_age_secs
        self._max_rss_mb = max_rss_mb
        # Which processes the ceiling above is measured over. None = the whole
        # descendant subtree, which is every kiro-family host. The spawn plan carries
        # a bounded host's depth relative to the exact pid Crew launches.
        self._max_rss_depth: int | None = None

        # session/new + session/load budget — resolved lazily on first use
        # (never in __init__: KiroCrewConfig.load() is a synchronous disk
        # read + schema validation on a cache miss, and runtimes are
        # constructed on the event loop) and cached for the runtime's
        # lifetime. See _session_start_budget().
        self._session_start_timeout: float | None = None

        # Process state
        self._process: asyncio.subprocess.Process | None = None
        self._pid: int | None = None
        # The root's process-start identity, read once at spawn. It is what lets
        # an OBSERVED death retire the registry lines that name this process and
        # no other: the number alone can be recycled before the write lands.
        self._spawn_start_token: str | None = None
        self._start_time: str | None = None
        self._spawn_monotonic: float | None = None
        # pid -> (start_id, basename): the record shape session_pid verifies a
        # descendant's identity against before it signals one, so a recycled pid
        # is skipped. Written by _snapshot_descendants at spawn and on every
        # session start -- nothing populated it before, which left
        # _provider_descendant_records with only its live walk, and a walk can
        # run only while the root is alive.
        self._child_pids: dict[int, ChildRecord] = {}
        # Names THIS spawn of the shared child process (fresh per spawn, cleared
        # with the process) — the identity a resource minted by the child is
        # compared against later. See AcpClient.process_instance for why the
        # session id cannot serve: a resume reuses it on a new process.
        self._process_instance: str = ""
        # The scope unit name systemd ACTUALLY received for this spawn, empty when
        # no scope was created. Held separately from the token because the token
        # alone cannot answer that question: name_scope_unit degrades to a no-op on
        # a host without cgroup delegation, so deriving the name from the token at
        # log time would announce a scope that does not exist. Set from the argv
        # the wrap returned, and cleared with the process.
        self._scope_unit: str = ""

        # Single reader task — the ONLY coroutine that reads stdout
        self._reader_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._stderr_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # Watches the root process's OWN exit, independent of the stdout stream.
        # The reader loop observes a death only as EOF on stdout, which does not
        # arrive while a descendant holds the root's inherited write end, so a
        # dead-but-unreaped root is invisible to its waiters for the life of the
        # survivor. This task fails them on the exit itself.
        self._exit_watch_task: asyncio.Task | None = None  # type: ignore[type-arg]

        # Demux routing
        self._pending_requests: _PendingRequests = _PendingRequests()
        # Method and send time of each awaited request in _ONE_AT_A_TIME_METHODS.
        # An id stays listed only while _pending_requests still holds it, so a
        # timed-out start that a collector adopted still counts.
        self._one_at_a_time_sent: dict[int, tuple[str, float]] = {}
        # session/new requests whose caller timed out but whose answer is still
        # owned (RFC §4.4); keyed by request id, settled by the collector.
        self._start_collectors: dict[int, StartCollector] = {}
        # Cleanup deadline for those collectors; resolved off-loop like the
        # session-start budget and cached for the runtime's lifetime.
        self._start_collect_timeout: float | None = None
        # Maps req_id → sessionId for responses that should be routed to a session queue
        # (e.g. session/prompt response signals turn completion and must reach the session)
        self._routed_requests: dict[int, str] = {}
        self._session_queues: dict[str, asyncio.Queue[JsonRpcMessage | None]] = {}
        # sessionId -> the ``model`` kiro-cli's last ``_kiro.dev/agent/switched``
        # named for it. ``session/set_mode`` answers ``{}``; this notification,
        # sent before that answer, is the only report of the model the activated
        # agent's spec put the session on. Read by ``_served_default_after_mode``.
        self._switched_models: dict[str, str] = {}
        # OAuth notifications can precede the session/new or session/load
        # response that reveals which queue to register. Stage only those
        # frames while an init is active, then transfer the matching session's
        # frames into its queue. The bounded buffer is cleared when the last
        # concurrent init finishes so an abandoned URL cannot reach a later
        # session that happens to reuse the same id. A start whose caller timed
        # out keeps its own copy on its StartCollector instead, so this deque
        # never outlives the init scope whose progress it describes (see
        # _stage_init_frame).
        self._session_inits_in_flight = 0
        self._pending_init_notifications: deque[JsonRpcMessage] = deque(
            maxlen=_INIT_NOTIFICATION_BUFFER_LIMIT
        )
        self._next_id = 1
        self._initialized = False
        # Whether kiro-cli advertised session/load support in its initialize
        # response. Mirrors AcpClient._can_load_session — load_session() guards
        # on it so we never issue session/load against a backend that lacks it.
        self._can_load_session = False
        # promptCapabilities from the initialize response (e.g. {"image": true}).
        # Empty until the handshake completes, so callers fail CLOSED and send
        # text-only rather than guessing a modality the agent never advertised.
        self._prompt_capabilities: dict = {}
        # The whole ``agentCapabilities`` object from the handshake. Retained
        # because a host's session-level MCP array has to be narrowed against
        # what it actually advertised: sending an element whose transport it
        # never declared can make it refuse the entire session/new rather than
        # skip that one server. Empty before the handshake, and empty on a host
        # that advertises nothing -- which the harness must read as "nothing is
        # known", never as "nothing is supported".
        self._agent_capabilities: dict = {}
        # agentInfo.version from the initialize response: the version of the
        # binary THIS process is executing, which can differ from the one on
        # disk after an in-place upgrade. Empty until the handshake completes.
        self._agent_version = ""
        # Entitlement probe state (probe_advertised_models): single-flight lock
        # plus a short-TTL cache of the last non-empty answer.
        self._entitlement_probe_lock = asyncio.Lock()
        # One descendant pass at a time. Each pass reads the tree and rewrites
        # this root's block from what it read, so two overlapping passes race:
        # the one that finishes last wins, and if that is the OLDER read, the
        # newer pass's descendants are dropped from both the record and the file.
        # Sessions start concurrently on a shared runtime, so this is reachable.
        self._descendant_scan_lock = asyncio.Lock()
        # Two independent clocks so a failure never extends the life of an old
        # success: ``_result_at`` is stamped ONLY when a non-empty result is
        # stored (that result may be replayed until it expires), while
        # ``_attempt_at`` is stamped on EVERY completed attempt incl.
        # failure/empty (it suppresses re-opening a session within the TTL but
        # never replays a stale result).
        self._entitlement_probe_result_at = 0.0
        self._entitlement_probe_attempt_at = 0.0
        self._entitlement_probe_result: list[dict[str, str]] = []
        self._dead = False
        # Set only after kill confirms the root exited and no tracked
        # descendant survived. Consumers use it to decide whether a work
        # directory is safe to reclaim.
        self._process_tree_confirmed_dead = False
        self._death_summary: str | None = None
        # The composed summary's parts, so the post-reap amendment rebuilds the
        # line instead of editing its text -- a tail carrying this format's own
        # shape must never be mistaken for the status field.
        self._death_reason = ""
        self._death_label = ""
        self._death_tail = ""
        # Severity _mark_dead settled on, after its refuse-the-downgrade
        # guard. The post-reap amendment logs at the SAME severity, so an
        # operator filtering on WARNING never sees the death without the
        # exit code that followed it.
        self._death_expected = False
        # Set by ``_mark_dead(stdin_stalled=True)``: the runtime was condemned
        # because the child stopped reading stdin, with the child still alive.
        # Any prompt frame already in that pipe may yet be read, so every
        # session with an unanswered prompt reports its death as an ambiguous
        # delivery (``AcpSessionHandle.prompt_outstanding_on_stall``).
        self._stdin_stall_death = False
        # The sessions whose turn was active when that stall condemned the
        # runtime, frozen at ``_mark_dead`` before any turn's teardown runs.
        self._stall_turn_sessions: frozenset[str] = frozenset()
        self._last_activity: float = 0.0
        self._stderr_lines: list[str] = []
        # Latched auth-failure observation. ``_stderr_lines`` is a 20-line ring,
        # so on a noisy startup the auth line can be evicted before anything asks
        # about it — and the question is only ever asked LATER, once a request
        # times out or the runtime dies. Re-scanning a buffer that has dropped
        # the evidence answers "no auth problem", which is indistinguishable from
        # a real negative. Latch on arrival instead: an auth failure observed once
        # stays observed for the life of this runtime, which is correct because
        # nothing about a rejected credential un-rejects itself mid-process.
        self._saw_auth_failure = False
        # Latched sandbox-init-refusal observation, latched for the same reason
        # and at the same sink as the auth latch above: the question ("why is this
        # runtime dead?") is asked after the ring has already turned over on a
        # chatty startup, and a re-scan that misses the line answers "not a
        # sandbox problem" indistinguishably from a real negative.
        #
        # Its LIFETIME is narrower than the auth latch's, though, and deliberately:
        # this one is a verdict about THIS CHILD'S STARTUP, and it is spent once
        # startup has demonstrably SUCCEEDED -- which is the first session handle,
        # not the ``initialize`` handshake. Startup continues past the handshake
        # through ``session/new``, and a sandboxed MCP launcher that the child
        # starts for that session can refuse there: closing the window at the
        # handshake would leave every such refusal unclassified, on the very
        # translation site (``create_session``) added to catch it.
        #
        # See the clear in ``_finish_create_session`` and the arming guard in
        # ``_drain_stderr``.
        self._saw_sandbox_init_failure = False
        # Latched ``kiro-cli-chat`` delegate-launch-failure observation. Latched
        # at the same sink and for the same ring-turnover reason as the two above,
        # and with the SAME narrow lifetime as the sandbox-init latch: whether the
        # delegate launches is a verdict about THIS CHILD'S STARTUP, spent once a
        # session handle proves startup succeeded. A delegate that would not exec
        # fails before ``initialize`` completes, so the pre-``_first_session_ready``
        # window is where it is caught; see the arming guard in ``_drain_stderr``.
        self._saw_delegate_launch_failure = False
        # Whether a session handle has ever been produced on this runtime. The
        # startup window the two latches above arm in, and the reason it is a separate
        # flag from ``_initialized``: the handshake is the middle of startup, not
        # its end.
        self._first_session_ready = False
        # Which isolation layer wrapped this runtime's child, recorded by
        # ``_spawn_admitted`` off the argv its wrap returned. False until then --
        # also the safe default for the classifier, since a spawn that never
        # reached the wrap cannot have been refused by it.
        self._sandbox_wrapped_by_crew = False
        # The mask set this spawn asked for, so a trusted corroboration run can
        # exercise the same mounts rather than a weaker profile.
        self._sandbox_hidden_dirs: tuple[str, ...] = ()
        # Unroutable-frame drop accounting: (sessionId, method) → count since
        # the last flush, plus the monotonic timestamp of that flush (0.0 = no
        # window open yet; the first counted drop opens it). Written ONLY from
        # _reader_loop (the single stdout owner) and its flush helper, so a plain
        # dict needs no lock — asyncio.ensure_future(self._reader_loop()) is
        # called exactly once, in spawn(), and never re-entered.
        self._dropped_frames: dict[tuple[str, str], int] = {}
        # In-flight SEL audit tasks for auto-rejected permission requests;
        # held only to keep them alive (see _answer_unroutable_permission).
        self._audit_tasks: set[asyncio.Task] = set()
        # In-flight ANSWER tasks (the coroutines that write the rejection
        # response), tracked separately from the SEL audit tasks above: the
        # flood cap below must count only tasks that can block on stdin
        # drain() — audit tasks are short-lived thread offloads, and letting
        # them satisfy the cap would let a burst of ordinary audits trip a
        # false mark_dead that kills every multiplexed session.
        self._answer_tasks: set[asyncio.Task] = set()
        # Volume bound for in-flight auto-answer tasks. Each task can block
        # on stdin drain() against a backend that floods permission frames
        # while never reading its stdin — unbounded, that grows the task set
        # until the gateway OOMs. Awaiting an answer inline on the reader
        # would hand the same hostile backend a demux freeze for every
        # session, so the bound treats a capacity timeout as a dead pipe
        # (see _wait_for_answer_capacity).
        self._max_answer_tasks: int = 128
        # Bounded discrimination wait at the cap (see
        # _wait_for_answer_capacity):
        # small enough that a wedged pipe is condemned promptly, large enough
        # that a responsive backend's in-flight answers can complete.
        self._answer_cap_wait_secs: float = 5.0
        # Backend-internal subagent session ids, snapshotted from each
        # `_kiro.dev/subagent/list_update` frame (a FULL list every time, so
        # replacement — not accumulation — keeps it bounded and current).
        # Membership proves an unregistered sessionId is a real backend child.
        # `_subagent_owner` records WHICH registered session was the sole
        # consumer when the announce arrived — routing later requires the sole
        # queue to still be that exact session, so a warm-reused runtime whose
        # session was swapped can never inherit a stale child's approvals.
        # Both are cleared when the owning session unregisters.
        self._subagent_sessions: set[str] = set()
        self._subagent_owner: str | None = None
        # How many ids the LAST roster named past NATIVE_CHILD_ROSTER_CAP.
        # Non-zero means an announced child may be unrecognisable here, so a
        # permission request for an unknown session is audited under
        # _ROSTER_OVERFLOW_REJECT_REASON instead of being called unregistered.
        self._subagent_roster_overflow: int = 0
        # Roster-truncation LOG accounting, the same shape _note_dropped_frame
        # uses: the first truncated snapshot of an episode warns, every later
        # one inside that episode is tallied here and folded into a throttled
        # DEBUG summary. `_roster_overflow_repeats` counts those later
        # snapshots, `_roster_overflow_peak` is the largest tail any of them
        # named, and `_roster_overflow_summary_at` is the monotonic timestamp
        # of the last summary — 0.0 means no episode is open, so the next
        # truncation is loud again. Written only from the demux loop's snapshot
        # handler and from unregister_session, so no lock is needed.
        self._roster_overflow_repeats: int = 0
        self._roster_overflow_peak: int = 0
        self._roster_overflow_summary_at: float = 0.0
        # Sessions with an ACTIVELY CONSUMING prompt dispatch loop, marked by
        # AcpSessionHandle.prompt() around its dispatch (all exit paths,
        # including timeout/cancel/synthetic completion, unmark in a finally).
        # _routed_requests is NOT usable for this: it also holds set_mode /
        # steer / config request ids, and a timed-out prompt leaves its entry
        # until the backend response arrives — either would make routing
        # believe a consumer exists and park a child request unread.
        self._turn_active_sessions: set[str] = set()
        self._dropped_frames_flushed_at: float = 0.0

    def _pool_lookup(self, agent: str | None) -> tuple[str | None, dict[str, Any] | None]:
        """The overlay name *agent*'s pooled stubs are found under, and its spec snapshot.

        A launch copy's stubs are the app's REGISTERED spec's, kept in the overlay under
        its file stem (``<app>--<agent>``); its snapshot is the copy read by path at
        spawn. ``None`` for the snapshot means read it by name, as for any other agent.
        A launch copy this process did not publish has no overlay name: no stubs, rather
        than a bare-name match that another spec of that name could answer.
        """
        if self._launch_agent and agent == self._agent:
            from kiro_crew.apps.agent_launch import launch_source_stem

            return launch_source_stem(self._launch_agent), self._launch_copy_spec
        return agent, None

    def _map_launch_cwd_paths(self, data: object) -> None:
        """Point a frame's tool paths at its session's cwd, not the launch directory."""
        from kiro_crew.apps.agent_launch import map_process_cwd_paths

        params = data.get("params") if isinstance(data, dict) else None
        if not isinstance(params, dict):
            return
        session_id = params.get("sessionId")
        session_cwd = self._session_cwds.get(session_id) if isinstance(session_id, str) else None
        # A ``session/update`` carries the tool call under ``update``, a permission
        # request under ``toolCall``.
        for key in ("update", "toolCall"):
            map_process_cwd_paths(
                params.get(key), self._launch_cwd_roots, session_cwd or str(self._work_dir)
            )

    def _wire_mode_id(self, agent: str | None) -> str | None:
        """The id kiro-cli knows *agent* by: the launch copy for this runtime's own agent."""
        if agent and self._launch_agent and agent == self._agent:
            return self._launch_agent
        return agent

    def crew_agent_name(self, name: str) -> str:
        """*name* as Crew records it: this runtime's launch copy reads as its own agent."""
        if name and self._launch_agent and name == self._launch_agent:
            return self._agent
        return name

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def spawn_monotonic(self) -> float | None:
        """Monotonic time this process was spawned, or ``None`` before spawn."""
        return self._spawn_monotonic

    @property
    def entitlement_probe_result_at(self) -> float:
        """Monotonic time the stored probe answer arrived (0.0 before any).

        :meth:`probe_advertised_models` serves either a fresh answer or a replay
        of this stored one; in both cases the answer is dated by this clock, so a
        caller that stores what it was served dates its snapshot from here rather
        than from its own call time, which for a replay would be LATER than the
        data and would raise its own freshness floor above it.
        """
        return self._entitlement_probe_result_at

    @property
    def work_scratch_dir(self) -> Path | None:
        """The session tree's work directory this process exposes as ``$KIROCREW_SCRATCH``.

        The inherited directory when this runtime joined an existing tree, else
        its own allocation; ``None`` before spawn or when allocation failed. This
        is what a spawn made ON BEHALF of a session on this runtime -- a
        companion runtime, a dedicated subagent process, a recycled successor --
        is handed as its ``shared_scratch``, so the whole tree keeps one work
        directory however many processes it spans.
        """
        return self._shared_scratch or self._scratch_dir

    @property
    def process_instance(self) -> str:
        """Identity of the CURRENT child process instance (``""`` when none).

        Fresh per spawn, so equality distinguishes the process that minted a
        resource from any successor — including one that resumed the same ACP
        session id. Liveness is asked separately (:meth:`is_alive`).
        """
        return self._process_instance if self._process is not None else ""

    @property
    def acp_backend(self) -> str:
        """Which ACP backend this runtime's process speaks.

        Public because the backend has to survive being read back off a
        started provider: the runtime is the only object that still knows it
        once ``AcpProvider`` swaps its placeholder client for a session
        provider.
        """
        return self._acp_backend

    @property
    def _harness(self) -> HarnessAdapter:
        """This backend's strategy object -- the runtime's only per-host answer.

        Every per-host question is asked here, so a host added later answers all
        of them in one file rather than in scattered arms nothing points at.

        ``harness_for`` raises ``ValueError`` for a backend with no harness, and
        that is the intended outcome: silently inheriting kiro-cli's argv,
        protocol version and teardown verb would start a session that then
        behaves wrongly, which is far harder to attribute than a refusal here.
        """
        # ``getattr`` for the same reason the projection path uses it: a caller
        # that only needs the agent projection constructs a bare runtime with
        # ``object.__new__`` and sets the two fields it cares about, so the cache
        # slot may not exist. Same lazy resolution either way.
        harness = getattr(self, "_harness_resolved", None)
        if harness is None:
            harness = harness_for(self._acp_backend)
            self._harness_resolved = harness
        return harness

    @property
    def uses_kiro_identity_store(self) -> bool:
        """True when this runtime's process signs in from kiro-cli's own store.

        Membership in ``backends_retired_by_host_logout()`` (harness-parity
        H5/H14). ``AcpRuntime`` is not an ``LLMProvider``, but the identity-change
        sweep reaches shared runtimes as well as session providers, so it
        declares the same capability under the same name -- letting that sweep
        ask both families one question instead of probing private attributes.
        """
        return self._acp_backend in backends_retired_by_host_logout()

    @property
    def supports_image_prompt(self) -> bool:
        """True when the agent advertised ``promptCapabilities.image``.

        Fails closed: an un-handshaked or silent backend reports False, so the
        prompt path sends text only instead of an image block the agent may
        reject.
        """
        return bool(self._prompt_capabilities.get("image", False))

    @property
    def agent_version(self) -> str:
        """``agentInfo.version`` the agent reported at ``initialize`` (``""`` until then).

        This is the version of the binary the process is RUNNING, which is what
        a capability decision about a live session must key on: after an
        in-place kiro-cli upgrade the file on disk is newer than every process
        spawned before it.
        """
        return self._agent_version

    @property
    def stdin_stall_death(self) -> bool:
        """Whether this runtime died of a stdin stall, its child left alive."""
        return self._stdin_stall_death

    @property
    def process_tree_confirmed_dead(self) -> bool:
        """Whether teardown confirmed the root and every tracked child exited."""
        return self._process_tree_confirmed_dead is True

    # ── Lifecycle ──

    #: How long a revive waits for the failed child's reply tasks to stop once
    #: cancelled. None of them absorbs a cancellation, so this is only a bound.
    _REVIVE_ANSWER_DRAIN_TIMEOUT = 1.0

    #: How long the failed-start reap waits for SIGKILLed descendants to be
    #: reaped by their new parent before reporting them as survivors.
    _FAILED_START_REAP_WAIT = 1.0

    #: One retry for a descendant scan that came back empty. The spawned root
    #: is a launcher that forks the agent, which forks again, so a scan racing a
    #: cold start can legitimately see nothing. A single retry is enough because
    #: every later session start scans again. Class attribute so tests can zero
    #: it rather than pay it.
    _DESCENDANT_RESCAN_DELAY = 0.5

    #: Why a teardown reached nothing, per ``_root_identity`` verdict. "holds" is
    #: reachable here too: the tree kill ran and the root exited under it, which
    #: is a race, not an identity failure.
    _ROOT_UNREACHED_REASON_BY_VERDICT = {
        "holds": "its root exited between the identity check and the signal",
        "mismatch": "its number is another process's now",
        "unknown": "its identity could not be read",
    }

    # Grace window for SIGTERM before escalating, and the post-SIGKILL reap
    # window. Class attributes so tests can shrink them.
    _KILL_TERM_TIMEOUT = 5.0
    _KILL_REAP_TIMEOUT = 2.0
    # Cadence at which the exit watcher polls process.returncode. returncode is
    # set by _process_exited (the SIGCHLD/waitpid path) the instant the root is
    # reaped, independent of pipe disconnection, so a short poll notices the
    # exit even while a surviving descendant holds stdout open and no EOF or
    # broken-pipe wakes process.wait(). Small enough to keep the fail-fast
    # promise, large enough not to busy-spin the loop.
    _EXIT_WATCH_POLL_INTERVAL = 0.25

    # After the exit is confirmed, give the reader a bounded window to drain any
    # final frame the backend wrote just before exiting -- a completed turn's
    # response sitting in the pipe the reader has not pulled yet. The reader
    # stamps ``_last_activity`` on every frame, so progress is observable: keep
    # yielding while it keeps draining, stop once it has been quiet for one grace
    # slice (nothing left to pull) or the total cap is reached. The cap bounds
    # the fail-fast delay a genuinely-empty pipe adds; the per-slice yield lets
    # the reader run between checks.
    _EXIT_DRAIN_GRACE = 0.05
    _EXIT_DRAIN_MAX = 1.0

    @property
    def reattempts_mcp_servers(self) -> bool:
        """Whether this host can connect one failed MCP server of a session again.

        The capability :meth:`begin_mcp_reattempt` checks first; a session asks
        it to decide whether a credential failure is still being re-attempted
        or should be named for a new session.
        """
        return bool(self._harness.opens_external_urls) and not self._dead

    @property
    def sandbox_wrapped_by_crew(self) -> bool:
        """Whether Kiro Crew's own sandbox wrapped this runtime's child.

        The one fact a sandbox refusal's stderr cannot carry: WHICH layer to turn
        off. A harness's internal sandbox nested inside Crew's wrap fails with the
        harness's wording while the layer the operator must change is Crew's.
        """
        return self._sandbox_wrapped_by_crew

    @property
    def sandbox_mode(self) -> str:
        """The sandbox tier this runtime's child was spawned under."""
        return self._sandbox_mode

    @property
    def sandbox_hidden_dirs(self) -> tuple[str, ...]:
        """The extra path masks this spawn asked its sandbox for.

        Read with :attr:`sandbox_mode` when a trusted corroboration run has to
        rebuild the profile this spawn was actually refused under.
        """
        return self._sandbox_hidden_dirs

    # Rendered in place of a returncode that is not knowable YET. Both say
    # "no exit status", and an operator reading only the log cannot tell them
    # from a signal-killed child whose status was never captured -- which is
    # exactly the report this labelling answers.
    _RC_NOT_REAPED = "<not reaped>"
    _RC_NO_PROCESS = "<no process>"
