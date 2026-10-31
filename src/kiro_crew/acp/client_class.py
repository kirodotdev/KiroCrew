"""``class AcpClient``: the direct ACP client's constructor, state, properties and member table.

The class body binds each member an owner module defines (``set_model =
_client_model_selection.set_model``) and keeps the constructor, the properties, the class
defaults and the few members that stay class-resident. ``kiro_crew.acp.client`` re-exports
the class, which keeps that module as its ``__module__``: the facade is the import path
callers, pickles and reprs name.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import uuid
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew import pinned_fs
from kiro_crew.acp import client_claude_seed as _client_claude_seed
from kiro_crew.acp import client_claude_session as _client_claude_session
from kiro_crew.acp import client_copilot as _client_copilot
from kiro_crew.acp import client_elicitation as _client_elicitation
from kiro_crew.acp import client_lifecycle as _client_lifecycle
from kiro_crew.acp import client_mcp_array as _client_mcp_array
from kiro_crew.acp import client_model_selection as _client_model_selection
from kiro_crew.acp import client_native_children as _client_native_children
from kiro_crew.acp import client_permissions as _client_permissions
from kiro_crew.acp import client_seed_ownership as _client_seed_ownership
from kiro_crew.acp import client_spawn_env as _client_spawn_env
from kiro_crew.acp import client_start as _client_start
from kiro_crew.acp import client_tool_events as _client_tool_events
from kiro_crew.acp import client_transport as _client_transport
from kiro_crew.acp import client_turn as _client_turn
from kiro_crew.acp import client_turn_control as _client_turn_control
from kiro_crew.acp import client_usage as _client_usage
from kiro_crew.acp._dispatch import (
    ACP_BACKENDS_META_IDENTITY,
    BackgroundLaunchRecord,
    InFlightIdSet,
)
from kiro_crew.acp.client_permissions import _scrub_observed
from kiro_crew.acp.client_start import CLIENT_NAME
from kiro_crew.acp.client_timeouts import _CANCEL_GRACE_SECS
from kiro_crew.acp.client_turn_control import _CLIENT_STEERING_REQUEST_METHODS, _ConcurrentSteerTurn
from kiro_crew.acp.elicitation import ElicitationRequest
from kiro_crew.acp.liveness import LivenessOracle
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.mcp_startable import PrunedServer
from kiro_crew.acp.runtime_models import DEFAULT_MODEL
from kiro_crew.acp.runtime_process_tree import ChildRecord
from kiro_crew.acp.skill_reads import SkillReadTracker
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKEND_STRANDS,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_MEMBER_CAPABILITIES,
    ACP_BACKENDS_SEED_LOCAL_SETTINGS,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STEER_CONCURRENT_PROMPT,
    ACP_BACKENDS_STEERING_REQUEST,
    AcpEvent,
    AcpPromptStats,
    JsonRpcMessage,
    model_registry_namespace,
)
from kiro_crew.agent import DerivedSpecSnapshot
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.config.paths import config_dir
from kiro_crew.mcp_gateway.claim import mint_stub_session_token

if TYPE_CHECKING:
    from kiro_crew.acp.harness.pi_steer import PiSteerChannel
    from kiro_crew.acp.session_mcp import ToolsAllowlist
    from kiro_crew.acp.skill_projection import NativeSkillProjection

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


class AcpClient:
    """JSON-RPC 2.0 client over stdio with kiro-cli acp."""

    # The facade is the class's import path, so pickles and reprs keep naming it.
    __module__ = "kiro_crew.acp.client"

    # Written by the claude adapter at launch, read back by the settings writer
    # through ``getattr`` with an empty default: never set on any other host.
    _claude_adapter_disk_version: str
    # Fields first set by a member an owner module defines, declared so the class names them.
    _claude_shipped_restrictions: frozenset[tuple[str, str]]
    _entitlement_probe_inflight: asyncio.Future[tuple[list[dict[str, str]], float]] | None
    _member_dispatch_mounted: bool
    _session_mcp_excludes_local: bool
    _session_mcp_inline_settings: dict[str, Any] | None
    _stdin_lock: asyncio.Lock
    _steering_waiter_map: dict[int, asyncio.Future[Any]]
    _unanswered_steering_id_set: set[int]
    _windows_tree_cleanup_failed: bool
    if TYPE_CHECKING:
        _native_skill_projection: NativeSkillProjection | None
    # The scrub this session applies to what a harness reports back, handed to the
    # host adapters that quote such a value (``ProcessSession._scrub_observed``).
    _scrub_observed = staticmethod(_scrub_observed)

    # Idle reader state (see _maybe_start_idle_reader). Class-level so a client
    # built without __init__ reads the idle defaults.
    #: Called with this client when the agent starts a turn on its own between
    #: prompts. The callee then reads that turn with ``stream_unsolicited``.
    on_agent_turn: Callable[[AcpClient], None] | None = None
    _idle_reader: asyncio.Task[None] | None = None
    # Readers currently claiming stdout; the idle reader runs only at zero.
    _stdout_claims: int = 0
    # The frame the idle reader saw an agent-started turn begin with, which it
    # left in _buffer (see _agent_turn_pending).
    _agent_turn_start: JsonRpcMessage | None = None
    # _install_idle_reader has bound its steps on this client.
    _idle_reader_installed: bool = False
    # Held by stream_unsolicited while it reads; a prompt waits on it (see
    # _install_idle_reader).
    _agent_turn_gate: asyncio.Lock | None = None
    # Prompts past that gate and not yet finished.
    _prompts_in_flight: int = 0

    def __init__(
        self,
        work_dir: str | Path | None = None,
        model: str | None = None,
        agent: str = CLIENT_NAME,
        sandbox_mode: str = "auto",
        session_key: str | None = None,
        channel_id: str | None = None,
        extra_env: dict[str, str] | None = None,
        acp_backend: str = "",
        audit_source: str | None = None,
        mcp_gateway_overlay: str | Path | None = None,
        mcp_gateway_socket: str | Path | None = None,
        permission_mode: str | None = None,
        shared_scratch: Path | None = None,
        side_read_only: bool = False,
    ):
        if work_dir:
            self._work_dir = Path(work_dir)
        else:
            self._work_dir = config_dir() / "workspace"
        # Once-per-instance guard for the ensure_ready work-dir check: True
        # after the first (off-loop) mkdir, so the per-prompt warm path pays
        # no filesystem syscall at all.
        self._work_dir_ready = False
        self._model = model or DEFAULT_MODEL
        self._agent = agent
        self._sandbox_mode = sandbox_mode
        self.memory_mode = "persistent"
        self._acp_backend = acp_backend
        # Claude backend permission mode (Auto-mode / permission-UI parity).
        # Inert on the kiro-cli path. None = the backend's own default
        # ("default", i.e. every tool decision is forwarded to the host), which is
        # what _write_claude_local_settings leaves in place when nothing asked
        # for a mode.
        self._permission_mode = permission_mode
        # A Side Chat session that may run read-only tools. On claude it starts
        # with only the read built-ins and no MCP server, and its mode is pinned
        # to ``default`` (_claude_session_meta, _pin_claude_starting_mode). Inert
        # on the kiro-cli path, where the derived ``<agent>--readonly`` spec
        # confines the session instead.
        self._side_read_only = side_read_only
        # That session's sensitive-path deny rules, built off the loop before its
        # launch (_prepare_claude_side_read_denies). Empty until then, and a side
        # session whose envelope would carry none is not started.
        self._claude_side_read_denies: tuple[str, ...] = ()
        # The session tree's work directory when this client is a DEDICATED
        # subagent process spawned on a parent's behalf: re-validated at spawn
        # (``agent_scratch.shared_scratch_window``) and mounted as a second
        # private window beside this process's own scratch, which keeps the
        # temp triple and the kiro-cli log. ``None`` for a session that starts
        # its own tree. Once live, the client joins the tree's owner marker
        # beside its parent (``agent_scratch.adopt_owner``), so a parent that
        # dies first leaves no dead-owner marker over its running children.
        self._shared_scratch: Path | None = Path(shared_scratch) if shared_scratch else None
        self._scratch_dir: Path | None = None
        # True once this session has CREATED <work_dir>/.claude/settings.local.json
        # itself. Only then does reset remove it, and only then does a re-seed
        # overwrite it. The writer refuses a path that already holds a file it did
        # not author, so Crew never owns the undo for someone else's project
        # settings -- see _write_claude_local_settings.
        self._claude_settings_authored = False
        # True while the project owns settings.local.json and this session leaves
        # that file out of its setting sources, carrying Crew's settings inline in
        # ``_claude_inline_settings`` instead -- see _exclude_foreign_local_settings.
        self._claude_local_settings_excluded = False
        self._claude_inline_settings: dict[str, Any] | None = None
        # The exact bytes this session last wrote to that path, held as the str
        # whose utf-8 encoding IS those bytes -- the writer emits binary so no
        # newline translation can come between the two on any platform. Creating
        # the file is not sufficient ownership on its own: a user can replace it
        # atomically (write-temp + rename) after the create, and the replacement is
        # theirs. So the flag says "Crew created it" and this says "and it is still
        # Crew's content" -- the re-seed and the reset unlink both require BOTH.
        self._claude_settings_written: str | None = None
        # True while a durable OWNER holder of this client's is still on disk for a
        # file whose bytes are not Crew's: a user replaced the seed and the hand-back
        # (``forget``, then ``release``) could not reach the sidecar. Authorship
        # drops the moment the bytes are observed foreign -- governance is about the
        # file, not the record -- so this carries the one thing teardown still owes:
        # another attempt at the hand-back. It feeds NO governance decision.
        self._claude_settings_claim_unrevoked = False
        # True while this session holds a durable reader lease on a sibling
        # Crew client's seed. The lease pins that file's lifecycle even after a
        # later byte re-validation fails, so it is deliberately distinct from
        # whether the current bytes still govern this session's permission
        # surface. A sharer never removes the owner's file or live claim.
        self._claude_settings_shared = False
        # True only while the sibling seed's CURRENT on-disk bytes have passed
        # byte-for-byte validation against this session's rendered permission
        # surface. Unlike the retained reader lease above, every re-validation
        # clears this flag on entry and only full success re-earns it; the MCP
        # array is withheld after every non-success exit.
        self._permission_surface_share_validated = False
        # Identity this client claims its seed under, in the durable record. Two
        # keyless clients share the default work_dir, so a token is what keeps
        # "Crew wrote it" from collapsing into "any Crew client may take it": the
        # durable record is for adopting an ORPHAN, and a sibling still running in
        # this process does not have one. Per instance and never reset -- a client
        # that re-spawns is the same owner across spawns.
        self._seed_owner = uuid.uuid4().hex
        # This session's translated ``mcpServers`` array, resolved once per spawn.
        # Held here so the shared session-params call site is a pure in-memory
        # read: the translation touches disk, and doing that AT the call site
        # would put an executor hop on every backend's construction path
        # (harness-parity H13). None = not resolved yet; cleared on reset so the
        # next spawn re-reads the spec. See _session_mcp_servers.
        self._session_mcp_cache: list[dict[str, Any]] | None = None
        # The ``DerivedSpecSnapshot`` that array was built from, or None when this
        # session's agent mirrors nothing. For an array-backed host the array IS the
        # spec the host consumes -- it reads no spec of its own -- so the bracket the
        # kiro-cli path closes at ``initialize`` closes HERE at the ``session/new`` /
        # ``session/load`` response instead, against this snapshot. Resolved and
        # cleared together with the array, for the same per-spawn freshness reason.
        self._session_mcp_snapshot: DerivedSpecSnapshot | None = None
        # The spec the array above was built from -- what an enrolled member's
        # session consumed. Read AFTER the session exists (by
        # :meth:`confirm_member_projection`, from the adapter-only arm of the client
        # startup path), so unlike the snapshot it survives a re-seed that drops the
        # array cache: the host already holds the array this describes, and dropping
        # the cache for the NEXT session must not erase what THIS one consumed.
        self._session_agent_spec: dict[str, Any] | None = None
        # A member session's spec ``tools`` allowlist on a mirror-less array host
        # (pi), from the same parse; the pooled stubs pass through it.
        self._member_stub_allowlist: ToolsAllowlist | None = None
        self.member_context = False
        self._loaded_capability_template = ""
        self._capability_projection_gaps: tuple[str, ...] = ()
        # Session id :meth:`confirm_member_projection` already confirmed, so a start
        # that re-enters on a live session does not judge it again. Cleared with the
        # session, never by the re-seed, which happens DURING the start that confirms.
        self._confirmed_capability_session = ""
        # This session's agent spec, snapshotted once per spawn for the
        # unresolved-ref guard alone (see _guard_unresolved_mcp_refs). Held for
        # the same reason as the array above and read at the same kind of site:
        # the guard runs where the wire array is composed, which is shared with
        # kiro-cli, so the read cannot happen there. None = not snapshotted (or
        # unreadable), which makes the guard a no-op rather than a disk read on
        # the loop. Cleared on reset so the next spawn re-reads the spec.
        self._mcp_ref_spec: dict[str, Any] | None = None
        # What the pre-spawn freshness gate verified for THIS child, captured in
        # _spawn and re-verified in _initialize_session once the child has read its
        # spec. _spawn is the only writer and it runs before any handshake, so the
        # value the post-load half reads always belongs to the live process.
        # Deliberately NOT cleared on reset: a retained snapshot can only ever be
        # over-strict, while a None substituted there would short-circuit the
        # re-check on any path that reached it. None = no spawn yet.
        self._derived_spec_snapshot: DerivedSpecSnapshot | None = None
        # The mirror's CLIENT OBLIGATION from the same spec parse as the array
        # (``SessionProjection.denied_tools``): ``(server, tool)`` pairs the spec
        # switched off that the backend cannot refuse on the wire, so this client
        # refuses them when the backend asks permission (``_deny_spec_disabled_tool``).
        # A backend that honours the restriction natively (kiro-cli) or through a
        # file Crew writes (claude's ``permissions.deny``) leaves it empty, and an
        # empty set makes the refusal a no-op. Cleared on reset with the array.
        self._spec_denied_tools: frozenset[tuple[str, str]] = frozenset()
        # The mirror's other CLIENT OBLIGATION from the same parse
        # (``SessionProjection.zero_tools``): the spec declares ``"tools": []``, a
        # total ban the array and ``denied_tools`` above have no way to express --
        # they answer "which MCP servers mount", not "may this agent's own native
        # tools (the harness's built-in Bash/Read/Write) run at all". kiro-cli/KAS
        # honour the ban natively; every mirrored backend does not, so this client
        # refuses EVERY permission request when it is set (``_handle_permission``).
        # Cleared on reset with the array.
        self._spec_zero_tools: bool = False
        # Narrowed servers the projection withheld because THIS session cannot put
        # their per-tool rule in force (``SessionProjection.unhonoured_servers``). No
        # member mount may re-add one, whatever the backend's PerToolDeny says.
        self._session_mcp_unhonoured: frozenset[str] = frozenset()
        # Tool ids the projection asks the harness itself to deny
        # (``SessionProjection.harness_deny_rules``), seeded by the opencode routing.
        self._session_harness_deny_rules: tuple[str, ...] = ()
        # The subset of those rules the opencode read-back found IN FORCE, handed back
        # to every later projection so it withholds a server whose rule is not. None
        # until this spawn has seeded any.
        self._opencode_denies_in_force: frozenset[str] | None = None
        # The seeded rules the last read-back found outranked by another rule.
        self._opencode_denies_unenforced: frozenset[str] = frozenset()
        # Servers opencode mounts from its own config, from this spawn's read-back.
        self._opencode_native_mounts: frozenset[str] = frozenset()
        # The MCP server names opencode's routing read-back found in the harness's
        # own resolved config, so a fused tool title from one of them splits back
        # exactly. Recorded by its launch (``acp.harness.opencode``).
        self._opencode_config_mcp_servers: tuple[str, ...] = ()
        # The per-session nonce a gate extension echoes in every dialog it raises;
        # minted by the pi, DeepSeek Harness or Strands launch, placed in the child's
        # environment there, and the only key under which a permission frame is read
        # as an envelope.
        self._pi_gate_nonce = ""
        # The pi MCP handoff file the launch published and its bytes, kept so it can
        # be republished before each request that starts a pi process and removed
        # once the session is established (``acp.harness.pi``).
        self._pi_mcp_handoff: tuple[str, bytes] = ("", b"")
        # The pi launch's steer channel (``acp.harness.pi_steer``), or None, and the
        # client's state for it (``acp.pi_steer_turn``): the open prompt turn, the
        # steers written into it by sequence number, and the write/close lock.
        self._pi_steer_channel: PiSteerChannel | None = None
        self._pi_steer_turn: int | None = None
        self._pi_steers_pending: dict[int, str] = {}
        self._pi_steer_lock = asyncio.Lock()
        # The launch MCP config file this spawn wrote, removed once the session
        # exists (``acp.harness.copilot``, ``acp.harness.strands``).
        self._copilot_mcp_config_path = ""
        # Length of the notification buffer when the routing write answered: the
        # config reports below it were superseded by that answer.
        self._routing_answer_floor = 0
        # Mode reports the start's drain read, kept for the drift check after it.
        self._drain_mode_reports: list = []
        # Every server name that handoff carried (the member mounts AND the pooled
        # stubs), which is what a pi MCP tool title is spelled after.
        self._pi_mcp_server_names: tuple[str, ...] = ()
        self._deepseek_gate_nonce = ""
        # Why this DeepSeek Harness session's READ_ONLY turns may not approve its
        # read tools by name, or ``""`` once its launch proved stock dsh composes
        # them (``tool_gate.deepseek_read_only_owner_issue``). Refused until then.
        self._deepseek_read_only_issue = "the harness composition was not read back"
        # toolCallIds the gate extension asked about in this session, read off the
        # envelopes; a completed tool call not in it is a call the gate never saw.
        self._pi_gate_asked_ids: set[str] = set()
        # request id -> toolCallId for every envelope the gate asked about, so a
        # reject answered by request id can name the call it denied; and the set
        # of denied calls, which must never reach ``completed``.
        self._pi_gate_request_tool: dict[str, str] = {}
        self._pi_gate_denied_ids: set[str] = set()
        self._session_key = session_key
        # When set, this client emits a per-tool-call SEL audit from the ACP
        # dispatch loop. Used by app/worker-pool clients (e.g. code-review-sage,
        # knowledge llm_pool) that have no external audit loop. Left None for
        # chat / subagent clients, which already audit via chat_runner /
        # SubagentManager, so they never double-log.
        self._audit_source = audit_source
        self._channel_id = channel_id
        self._extra_env = extra_env or {}
        # ``agent.env`` for the spawn in progress; see _agent_env_for_spawn.
        self._agent_env_snapshot: dict[str, str] | None = None
        # MCP gateway overlay: when set, the broker stubs in its rewritten specs
        # are injected into this session at ACP session/new, where they outrank
        # the same-named entries in the agent spec. Nothing is written to the
        # user's project or to ~/.kiro/agents. None = pooling off.
        # Broker requests carry the owning session through ordinary transport
        # authentication; member memory uses that session's execution record.
        self._mcp_gateway_overlay = str(mcp_gateway_overlay) if mcp_gateway_overlay else None
        self._mcp_gateway_socket = str(mcp_gateway_socket) if mcp_gateway_socket else None
        # Token this client's injected broker-stub entries carry, so gatewayd can
        # tell this session's stub connections from those of another session on
        # the same runtime PID (``mcp_gateway.claim.mint_stub_session_token``).
        # Minted once per client: one client drives one child process serving one
        # session at a time, and a warm-pool ``rekey()`` re-binds the SAME token
        # to the claiming session rather than re-minting — the stub processes
        # outlive a single chat, so a fresh token would leave the live ones
        # carrying a name no claim will ever mention again. Never logged.
        self._stub_session_token = mint_stub_session_token()
        self._sandbox_cleanup: str | None = None
        self._bound_workspace_fd: int | None = None
        self._spawn_work_dir = str(self._work_dir)
        self._process: asyncio.subprocess.Process | None = None
        self._pid: int | None = None
        self._kill_sent: tuple[int, str] | None = None  # (signal, reason) we last sent
        # The root's process-start identity, read once at spawn and handed to
        # both the session-file tracker and the identity-bound retirement in
        # _reset_state, so the line written and the line later compared are one
        # read of the identity rather than two (see session_pid._pid_start_token).
        self._spawn_start_token: str | None = None
        # False until shutdown confirms both the root's exit and every tracked
        # descendant's exit. A work-directory reclaim reads this fail-closed
        # verdict after shutdown.
        self._process_tree_confirmed_dead = False
        self._start_time: str | None = None  # start identity for PID-recycle detection
        # Names THIS spawn of the child, not the session it serves: a resume
        # re-uses the session id on a brand-new process (see ensure_ready's
        # session/load path), so the session id cannot distinguish the process
        # that minted a resource from a successor that cannot honor it. Fresh
        # per spawn, cleared with the process. See ``process_instance``.
        self._process_instance: str = ""
        self._session_id: str | None = None
        self._next_id = 1
        self._buffer: deque[JsonRpcMessage] = deque(maxlen=100)
        self._mcp_notifications: list[JsonRpcMessage] = []
        # What THIS session's MCP servers reported at init. The frames arrive
        # during _drain_notifications; reducing them to one log line and dropping
        # them would leave a session that started without a server unable to say
        # so. Read via mcp_session_report().
        self._mcp_report = McpSessionReport()
        #: Index into ``_mcp_notifications`` below which frames belong to a PRIOR
        #: session attempt and must not reach the report. See
        #: ``_begin_session_report``.
        self._mcp_report_frame_floor = 0
        #: The array the last session attempt sent, and when, for the session-open
        #: mount check. Set by ``_begin_session_report``; see ``_start_mount_check``.
        self._mcp_mount_servers: list[Any] = []
        self._mcp_mount_opened_at: float | None = None
        #: Elements the last array build left out as unstartable, recorded on the
        #: report by ``_begin_session_report``. See ``_prune_unstartable_mcp``.
        self._mcp_pruned: tuple[PrunedServer, ...] = ()
        #: Names of the servers this attempt pruned, set by ``_begin_session_report``.
        #: The ref guard counts them as configured: each is already on the report as
        #: failed, and calling it unconfigured too would point at the projection.
        self._mcp_pruned_names: tuple[str, ...] = ()
        #: How many pruned servers the last array build had past
        #: ``MCP_REPORT_BUCKET_CAP``; counted on the report, never listed.
        self._mcp_pruned_omitted = 0
        #: The MCP array a harness was given at LAUNCH rather than on the wire, or
        #: ``None`` when the wire array is the whole set. Strands' Python-project
        #: mode refuses a non-empty ``session/new`` array, so its servers ride the
        #: ``--mcp-config`` file; the session report and the unresolved-ref guard
        #: read this instead of the empty wire array. Set by the Strands adapter
        #: (``acp/harness/strands.py``) at each spawn.
        self._launch_mcp_servers: list[Any] | None = None
        # MCP OAuth requests collected during session init from
        # `_kiro.dev/mcp/oauth_request` notifications. Drained by callers via
        # `pop_pending_oauth_requests()` after `ensure_ready()` so the UI can
        # surface an Authorize button to the user. Each entry: {"serverName", "oauthUrl"}.
        self._pending_oauth_requests: list[dict[str, str]] = []
        # Server names already surfaced to the UI in this ACP session — kiro-cli
        # may emit `_kiro.dev/mcp/oauth_request` multiple times per server (e.g.
        # once per probe attempt). Dedupe so the user sees one banner per server.
        self._oauth_emitted_servers: set[str] = set()
        self._cancelled = False
        self._cancel_ts: float = 0.0
        # Cooperative-cancel read-grace for the CURRENT cancel. Defaults to the
        # module floor but is raised to the caller's ack budget by
        # cancel_session() so a configured soft_stop_budget_secs > 10 actually
        # extends the window instead of being silently capped (the read loop
        # would otherwise abort the turn at 10s while the soft waiter blocked
        # the full budget, then hard-kill — losing the session).
        self._cancel_grace_secs: float = _CANCEL_GRACE_SECS
        self._resume_session_id: str | None = None
        # (source session id, fork-point message id) for the next ensure_ready().
        self._fork_request: tuple[str, str] | None = None
        self._resumed = False
        self._can_load_session = False
        # promptCapabilities.image from the initialize response. Fails closed:
        # until a handshake says yes, no image block is sent.
        self._agent_accepts_image = False
        # agentInfo.version from the initialize response — the version the
        # spawned process runs, not the file on disk. "" until the handshake.
        self._agent_version = ""
        # Whether ``_agent_version`` has been read off a handshake at all, so an
        # adapter that reported NO version is told apart from one not yet asked.
        self._agent_version_read = False
        # Models advertised by the backend in the session/new (or session/load)
        # response. claude-agent-acp returns the real versioned Claude list
        # (Opus 4.8/4.7, Sonnet 4.6, …); kiro-cli returns its own. Captured so
        # the dashboard model dropdown reflects what the backend actually
        # offers rather than a hardcoded guess. Each entry: {modelId, name,
        # description}.
        self._available_models: list[dict[str, str]] = []
        # When that snapshot was captured (monotonic) and whether a live re-probe
        # confirmed it. A ``session/new`` answer is one unconfirmed reading taken
        # inside the startup race where an entitlement lookup can answer the
        # free-tier default; only :meth:`refresh_available_models` confirms it.
        self._available_models_captured_at: float = 0.0
        self._available_models_probe_confirmed: bool = False
        # Set by _capture_available_models (claude only) when the discovered ids
        # changed the cross-session provider-model cache, signalling the async
        # init path to offload a disk persist. Reset to False after each persist.
        self._advertised_models_changed: bool = False
        # Mode ids the backend advertised at session init (session/new|load
        # `modes.availableModes`). Empty when the backend omits `modes` (older
        # kiro-cli / offline fake) — the set_mode guard treats empty as "attempt"
        # for backward compatibility. Populated by _store_session_config.
        self._available_mode_ids: list[str] = []
        # Whether the backend advertised a `modes` list at all (even an empty
        # one). Distinguishes "unknown, attempt for backward compat" (False)
        # from "advertised zero/some modes, honor the list" (True) so an
        # explicitly-empty availableModes fails closed rather than attempting.
        self._modes_advertised: bool = False
        # Model kiro-cli/claude-agent-acp actually resolved to (may differ
        # from self._model when that's the "auto" sentinel). It keys the lookup of
        # the context window when usage_update isn't sent (see _track_metadata).
        self._resolved_model_id: str | None = None
        # The model a ``_kiro.dev/agent/switched`` frame says the session now
        # runs, waiting to be judged by ``_judge_activated_model`` before the next
        # prompt. ``""`` = nothing to judge. See ``_note_agent_switched``.
        self._activated_model: str = ""
        # True from the start path's ``session/set_mode`` until the first
        # agent/switched frame after it, and whether a ``session/set_model``
        # went out in between (that later write is what the session runs).
        self._mode_frame_awaited: bool = False
        self._mode_frame_superseded: bool = False
        # Model the backend last substituted to via the -32603 admin-tier policy
        # advisory ("Using X instead"). Set by _wait_for_response when it sees the
        # advisory; consumed by the session/new path to re-issue creation on the
        # model the gateway will actually serve (the advisory carries no
        # sessionId, so the first attempt creates nothing). None = no substitution.
        self._last_substitution_model: str | None = None
        # The pinned model a startup config-option push was refused on, or a
        # startup pin kiro withheld because the account does not serve it, or
        # ``""``. Neither raises: the session stays on the backend default.
        # Callers that bill or label a turn by the pin read this to learn the
        # pin never ran.
        self.model_pin_refused: str = ""
        # The bare model a ``<model>[<effort>]`` pin landed as when its effort
        # half was refused, or ``""``. Set by ``_push_model_via_effort_split``.
        self.model_pin_partial: str = ""
        self._child_pids: dict[int, ChildRecord] = {}  # pid → (start_time, basename)
        # Claude Code's native subagents (``client_native_children``): the child ids
        # this session has seen, bounded by NATIVE_CHILD_ROSTER_CAP with the overflow
        # counted, one card row per id, the Agent call each delegation came from,
        # and the ids listed this turn, in arrival order.
        self._native_child_sids: set[str] = set()
        self._native_child_overflow = 0
        self._native_child_rows: dict[str, dict[str, Any]] = {}
        self._native_child_by_tool_use: dict[str, str] = {}
        self._native_child_turn: dict[str, None] = {}
        # One ``StreamRedactor`` per native child whose text is streaming, keyed by
        # its tracked id (so bounded by the same cap), flushed and dropped when the
        # roster reports the child done or failed (``client_turn``).
        self._native_child_redactors: dict[str, Any] = {}
        self.last_prompt_stats = AcpPromptStats()
        # Credits kiro reported while no turn was running (a compaction waited
        # on between turns), held for the next turn's bill; see _track_metadata.
        self._between_turn_credits = 0.0
        self._tool_call_inputs: dict[str, str] = {}
        # Same-key provenance for the redacted display cache.  No removed bytes
        # are retained here; approval surfaces only need to know that the value
        # they received was not the complete command the provider requested.
        self._tool_call_input_redacted: dict[str, bool] = {}
        # Map toolCallId → is_shell, cached from the tool_call notification so
        # the later permission_request event (which carries no kind) can inherit
        # the canonical shell signal. Mirrors _tool_call_inputs lifecycle.
        self._tool_call_is_shell: dict[str, bool] = {}
        # Map toolCallId → "no channel classified this call". A tool_call frame
        # answers the question "is this a command?" through an ACP ``kind`` or
        # through a harness ``_meta`` channel; a frame carrying NEITHER leaves the
        # command gates with nothing to check, and the permission request that
        # follows carries no kind of its own. Remembered here so that request can
        # be refused rather than answered blind. Mirrors _tool_call_is_shell's
        # lifecycle.
        self._tool_call_unclassified: dict[str, bool] = {}
        # Skill bodies this session's tool calls read directly, credited to the
        # usage ledger once each read completes. Shared with AcpSessionHandle so
        # the credit does not depend on the transport. Mirrors
        # _tool_call_is_shell's lifecycle.
        self._skill_reads = SkillReadTracker()
        # Map toolCallId → trusted MCP server name (_meta.kiro.mcpServerName),
        # cached from the tool_call notification so the later permission_request
        # event (which carries no _meta) can inherit it — the signal the
        # app-own-server auto-approve keys on. Mirrors _tool_call_is_shell.
        self._tool_call_mcp_server: dict[str, str] = {}
        # Map toolCallId → trusted tool name (_meta.kiro.toolName), cached like
        # _tool_call_mcp_server so the permission_request event can rebuild the
        # canonical mcp__<server>__<tool> for per-tool governance in the
        # app-own-server auto-approve.
        self._tool_call_tool_name: dict[str, str] = {}
        # toolCallId -> the tool's own name its tool_call frame stated, for the
        # permission event's harness_tool_id (see _dispatch.harness_tool_name).
        self._tool_call_harness_tool_name: dict[str, str] = {}
        # toolCallId -> True when the tool_call frame PRESENTED an MCP identity half
        # the name bound refused (``mcp_identity_unreadable``); the permission
        # request is then refused in ``_handle_permission`` rather than judged
        # under the empty name. Mirrors AcpSessionHandle._tool_call_identity_unreadable.
        self._tool_call_identity_unreadable: dict[str, bool] = {}
        # Structured raw tool params (rawInput dict) keyed by toolCallId, cached
        # from the ToolCall notification so the later request_permission event —
        # which carries only a truncated title — can recover the real path/url
        # the governance gate needs (filesystem.write / network.egress scopes).
        self._tool_call_params: dict[str, dict] = {}
        # toolCallId -> path named by the tool_call's diff content block, so the
        # permission event can carry diff_path for the edit gate when the params
        # themselves carry no path key. Mirrors AcpSessionHandle's cache.
        self._tool_call_diff_path: dict[str, str] = {}
        # Copilot CLI tool_call ids whose title more than one placed MCP server could
        # have produced; their permission request is refused.
        self._copilot_ambiguous_mcp_calls: set[str] = set()
        # Copilot CLI kind-``other`` tool_call titles no placed server name matched.
        self._copilot_unmatched_titles: dict[str, str] = {}
        # Map JSON-RPC request id → {"once": optionId, "always": optionId} so
        # the host can echo back the exact optionIds the agent advertised.
        # kiro-cli uses "allow_once"/"allow_always"; claude-agent-acp uses
        # "allow"/"allow_always". Falling back to OPTION_ALLOW_ONCE causes
        # claude-agent-acp to reject the response.
        self._permission_options: dict[str | int, dict[str, str]] = {}
        # Request id -> the permission event built for it, so approve_tool can
        # put the request through the security floor (``permission_floor``)
        # whichever consumer answers it.
        self._permission_gate_events: dict[str | int, AcpEvent] = {}
        # Consumers that can put an MCP elicitation to a person opt IN; for
        # everyone else the client declines the request itself before it
        # would be yielded (see client_elicitation). Request id -> the parsed
        # ask, for every elicitation yielded and not yet answered.
        self.accepts_elicitation: bool = False
        self._pending_elicitations: dict[str | int, ElicitationRequest] = {}
        self._stderr_lines: deque[str] = deque(maxlen=20)
        # Latched on this process's FIRST non-thinking text chunk, tool call or
        # tool result, cleared with the rest of the process state on respawn:
        # the registration-throttle death classification is refused once work
        # has been observed, so the transient verdict it hands the retry
        # ladders can only ever license replaying a turn that provably did
        # nothing. Per PROCESS (this client owns exactly one), matching the
        # ring the evidence is read from.
        self._prompt_or_tool_seen = False
        # Set by ``_spawn`` from the wrapped argv; only meaningful once a child
        # has been spawned. False before that, which is also the safe default for
        # the classifier: a spawn that never reached the wrap cannot have been
        # refused by it.
        self._sandbox_wrapped_by_crew: bool = False
        # The mask set this spawn asked for, so a trusted corroboration run can
        # exercise the same mounts rather than a weaker profile.
        self._sandbox_hidden_dirs: tuple[str, ...] = ()
        self._jsonl_pos: int = 0  # track read position in session JSONL for tool results
        self._stderr_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._last_activity: float = time.monotonic()
        # Set when a request write stalls (``_write_request_bounded``): the child
        # stopped reading stdin and may still read a buffered frame. The client
        # then reads as neither responsive nor mid-turn, and the next
        # ``ensure_ready`` kills it before spawning a replacement, so no new
        # prompt is ever appended behind the stalled one.
        self._stdin_stalled = False
        # Idle == done. An idle client (spawned, no prompt sent yet) must read
        # as NOT in a turn: has_active_turn() is the 409 turn_in_flight gate
        # on set-model / set-agent, so an unset Event on a live warm process
        # blocks the user until a real turn's finally runs. Every prompt
        # entry clear()s this before sending, so a real turn still reads active.
        self._turn_done: asyncio.Event = asyncio.Event()
        self._turn_done.set()
        # goose steer bookkeeping (``_steer_via_request``): the running prompt's
        # announced run id, steer requests awaiting their answer by request id,
        # and accepted steers awaiting their echo by goose ``messageId``.
        self._active_run_id: str | None = None
        self._steer_answers: dict[int, tuple[asyncio.Future[str], str]] = {}
        self._steers_awaiting_echo: dict[str, str] = {}
        # Steers sent as a second ``session/prompt``
        # (``ACP_BACKENDS_STEER_CONCURRENT_PROMPT``): the running prompt turn's own
        # record, and the earlier steers on this process never answered, which the
        # next prompt cancels before it is written (``_settle_orphan_steers``).
        self._steer_turn: _ConcurrentSteerTurn | None = None
        self._orphan_steers: set[int] = set()
        self._orphan_steers_process: Any = None
        self._turn_permission_denied = False
        # Serializes whole read turns on this client's single stdout StreamReader.
        # An asyncio StreamReader permits exactly ONE waiting reader; the shared
        # `_bg` session is streamed by ~8 callers and the per-session Semaphore(1)
        # does not cover abnormal-exit overlap (a turn dying mid-readline leaves a
        # parked read the next caller collides with -> "readuntil() called while
        # another coroutine is already waiting"). Acquired at the top of
        # _prompt_loop, released in its finally.
        #
        # Finalization caveat: a consumer that `return`s on "complete" without
        # exhausting _prompt_loop leaves the async-gen SUSPENDED; CPython runs
        # its finally via a *deferred* scheduled athrow (next loop tick), not at
        # the consumer's return. So the lock releases promptly on a live loop but
        # NOT synchronously. send_message_stream wraps the loop in `aclosing(...)`
        # to force deterministic release on its hot path; the other consumers
        # rely on the next-tick finalization.
        #
        # Coverage caveat: this lock only covers reads inside _prompt_loop.
        # _read_message also has callers OUTSIDE the loop (_wait_for_response
        # during init, wait_for_compaction). Those run in distinct lifecycle
        # phases that do not overlap a streaming _bg turn, so they are not
        # serialized here; if that ever changes, the readuntil race could recur.
        self._turn_lock: asyncio.Lock = asyncio.Lock()
        self._stale_eligible: bool = False  # set by _dispatch_events after text chunks
        # Set when a tool_call is yielded, cleared when the tool resolves
        # (tool_call_update result) or the turn starts/completes.  NOT cleared
        # on arbitrary inbound frames — that would disarm the watchdog after a
        # single progress frame.  Gates the _TOOL_STALL_TIMEOUT watchdog so a
        # dispatched-but-never-resolved tool can't hang the whole turn.
        self._tool_dispatched: bool = False
        # The open calls' ids, bounded like the session handle's attribution
        # map (``_dispatch.InFlightIdSet``: the shared key and count bound, the
        # oldest evicted first, evictions counted). After an eviction the set
        # answers ``possibly_open`` until the turn resets, so the tool-stall
        # clock (``_TOOL_STALL_TIMEOUT``) stays armed and the stale clock
        # disarmed for a call whose row was dropped but may still be running.
        self._active_tool_calls = InFlightIdSet()
        # Armed by _handle_compaction_status on a `failed` status and cleared at
        # turn start: gates the _COMPACTION_FAILED_TURN_BUDGET check in
        # _prompt_loop. _compaction_failed_turn records that the check fired, so
        # _dispatch_events ends the turn with the compaction stop reason instead
        # of the generic timeout error.
        self._compaction_failed_at: float | None = None
        # Retryability of the LAST failed compaction, read by the dashboard's
        # STOP_REASON_COMPACTION_FAILED branch to decide between re-queuing the
        # abandoned message and giving up. Public (no leading underscore)
        # because that consumer reaches it through getattr on whichever of the
        # two client classes is serving the slot. Only the VERDICT is carried:
        # the reason text reaches the chat row through the compaction-status
        # event title and the server log through the WARNING each arming site
        # already emits, so forwarding it as well would widen the provider
        # contract for no reader.
        self.last_compaction_transient: bool = False
        self._compaction_failed_turn: bool = False
        # Set when the claude backend's "Compacting..." notice is seen inside a
        # turn and cleared by its terminal notice. Only a MANUAL /compact gets a
        # terminal from the adapter (see parse_claude_compaction_notice), so an
        # automatic mid-turn compaction leaves this armed and the dispatch loop
        # settles it with a synthetic `completed` at turn end. Without that, a
        # consumer showing a compacting state would never leave it.
        self._claude_compaction_pending: bool = False
        # The codex twin of the flag above. It guards TWO directions, and the
        # second is why it is read on the way in as well as at turn end. A loaded
        # session replays a past compaction as a ``tool_call`` that is already
        # ``completed``, so only a terminal following a ``started`` seen in THIS
        # turn describes work this turn did. And a compaction that ERRORS reports
        # nothing at all -- codex-acp's ``runCompact`` never resolves, so the
        # ``session/prompt`` request goes unanswered -- which leaves this armed
        # for ``_settle_codex_compaction`` to close out at the turn's terminal.
        self._codex_compaction_pending: bool | str = False
        # When this session's harness last launched work that outlives the
        # prompt (a backgrounded command, a Workflow). Never reset per turn: a
        # turn ending says nothing about whether that work has finished, and
        # this client reads its pipe only while a call is waiting, so the
        # harness's eventual report of it is not seen until the next prompt.
        self._background_launches = BackgroundLaunchRecord()
        # Liveness oracle for the stale-turn gate: before ending a silent turn
        # at _STALE_TURN_TIMEOUT, consult /proc evidence so a backend that is
        # provably working (CPU/IO movement in the subprocess subtree) is not
        # reaped. Mirrors the kiro shared-runtime path (AcpSessionHandle), which
        # already defers on a WORKING verdict instead of a blunt wall-clock.
        self._liveness_oracle = LivenessOracle()
        # Keep the executor future, not an await-scoped flag: wait_for can time
        # out while the underlying thread continues its /proc walk. A pending
        # future prevents silent-read polling from stacking blocked workers.
        self._consult_future: asyncio.Future[tuple[str, str]] | None = None
        # Record every observed tool_call (id -> (title, kind)) so the
        # PostToolUse hook fire can recover the tool_name from the result's
        # tool_call_id — the RESULT event carries no title. See
        # _maybe_fire_post_tool_hooks.
        self._observed_tool_calls: dict[str, tuple[str, str]] = {}
        # This turn's tool-call ids whose SEL record is settled: the call asked the
        # host (its consumer writes the record) or the unasked-call audit wrote it.
        # See _maybe_audit_unasked_tool_call.
        self._sel_settled_tool_calls: set[str] = set()
        self._last_stop_reason: str = ""
        # Dynamic config from ACP session/new response and config_option_update notifications.
        # Only the effort configOptions are consumed (model lists come from
        # _capture_available_models, which parses the real dict-shaped `models`).
        self._acp_config_options: list[dict] = []

    @property
    def backend(self) -> str:
        """ACP backend identifier (e.g. ACP_BACKEND_CLAUDE for claude-agent-acp)."""
        return getattr(self, "_acp_backend", "")

    @property
    def agent_version(self) -> str:
        """``agentInfo.version`` reported at ``initialize`` (``""`` until then).

        The version this process RUNS — see :attr:`AcpRuntime.agent_version`.
        """
        return getattr(self, "_agent_version", "")

    @property
    def initialize_result(self) -> dict:
        """The agent's ``initialize`` result (``{}`` until then)."""
        return getattr(self, "_initialize_result", {})

    @property
    def spec_zero_tools(self) -> bool:
        """True once this session's CONSUMED projection confirms the spec's ``"tools": []``.

        Set from :attr:`_spec_zero_tools`, which only becomes True after the session's
        projection has been parsed and reports the agent spec actually in force declares
        an empty tool list, so :meth:`_deny_zero_tools` refuses every permission request
        the session raises. The projection is the mirror's, or for a mirror-less host
        whose array Crew composes (pi) the parse :meth:`_mirrorless_member_mounts`
        reads. Stays False for the lifetime of a session whose spec is missing,
        malformed, or shadowed by another one, and always False on kiro-cli, where the
        harness itself reads the spec and enforces the ban natively rather than through
        this client.
        """
        return self._spec_zero_tools

    # Compose an ``AcpClient`` session's ``mcpServers`` array: ``kiro_crew.acp.client_mcp_array``.
    authored_spec_declares_zero_tools = _client_mcp_array.authored_spec_declares_zero_tools
    effective_spec_declares_zero_tools = _client_mcp_array.effective_spec_declares_zero_tools
    _pooled_mcp_servers = _client_mcp_array._pooled_mcp_servers
    _resolve_session_mcp_servers = _client_mcp_array._resolve_session_mcp_servers
    _mirrorless_member_mounts = _client_mcp_array._mirrorless_member_mounts
    _pooled_broker_stubs = _client_mcp_array._pooled_broker_stubs
    _append_member_dispatch_server = _client_mcp_array._append_member_dispatch_server
    _member_mount_withheld = _client_mcp_array._member_mount_withheld
    _append_member_panel_server = _client_mcp_array._append_member_panel_server
    _withhold_is_the_only_deny_channel = _client_mcp_array._withhold_is_the_only_deny_channel
    _read_mcp_ref_spec = _client_mcp_array._read_mcp_ref_spec
    _guard_unresolved_mcp_refs = _client_mcp_array._guard_unresolved_mcp_refs
    _session_mcp_servers = _client_mcp_array._session_mcp_servers
    _claude_session_mcp_servers = _client_mcp_array._claude_session_mcp_servers
    _opencode_session_mcp_servers = _client_mcp_array._opencode_session_mcp_servers
    _deepseek_session_mcp_servers = _client_mcp_array._deepseek_session_mcp_servers
    _copilot_session_mcp_servers = _client_mcp_array._copilot_session_mcp_servers
    _pi_session_mcp_servers = _client_mcp_array._pi_session_mcp_servers
    _republish_pi_mcp_handoff = _client_mcp_array._republish_pi_mcp_handoff
    _discard_pi_mcp_handoff = _client_mcp_array._discard_pi_mcp_handoff
    _discard_copilot_mcp_config = _client_mcp_array._discard_copilot_mcp_config
    _goose_session_mcp_servers = _client_mcp_array._goose_session_mcp_servers
    _invalidate_session_mcp_projection = _client_mcp_array._invalidate_session_mcp_projection
    _prepare_session_mcp = _client_mcp_array._prepare_session_mcp
    _mcp_timeout_progress = _client_mcp_array._mcp_timeout_progress
    pop_pending_oauth_requests = _client_mcp_array.pop_pending_oauth_requests
    _begin_session_report = _client_mcp_array._begin_session_report
    _start_mount_check = _client_mcp_array._start_mount_check
    _prune_unstartable_mcp = _client_mcp_array._prune_unstartable_mcp
    mcp_session_report = _client_mcp_array.mcp_session_report
    wait_for_declared_mcp = _client_mcp_array.wait_for_declared_mcp
    _placed_mcp_server_names = _client_mcp_array._placed_mcp_server_names
    _harness_mcp_server_names = _client_mcp_array._harness_mcp_server_names

    @property
    def _enforces_zero_tool_ban(self) -> bool:
        """True when the spec's ``"tools": []`` is refused at the permission request.

        Needs both facts: the session's spec declares the empty list, and the backend
        honours it as a ban on native tools too. A backend that does not honour it
        leaves the refusal unreached on some paths, so it is not applied at the sites
        that do run.
        """
        return self._spec_zero_tools and capabilities_for(self.backend).honors_zero_tool_ban

    @property
    def _judges_permission_requests(self) -> bool:
        """True when a permission request on this session is JUDGED before it is answered.

        Two facts make an answering site build the full event and consult the provenance
        caches instead of approving on the frame alone: a spec deny set (a switched-off
        tool must be refused wherever it asks), and membership in
        ``ACP_BACKENDS_META_IDENTITY`` (the harness publishes a tool identity on every
        frame, and a frame that carries none, or one naming a server Crew never mounted,
        is refused). The second is a fact about the channel, not about the deny set: the
        goose projection carries an empty deny set by ruling, so a refusal that waited
        for one would never run on a goose session at all. Everywhere else the sites keep
        their prior behaviour byte for byte.

        A third fact judges too: :attr:`_enforces_zero_tool_ban`, the agent's own
        ``"tools": []`` on a backend that honours it. It is not folded into the
        deny-set check above because it answers a broader question -- every request,
        MCP or native -- and a spec with no MCP servers to deny at all (the ordinary
        case for a zero-tool agent) would otherwise leave ``_spec_denied_tools`` empty
        and this property False, which is exactly the auto-approve path the zero-tool
        ban exists to close.

        Copilot CLI judges too: it publishes no identity channel, so its MCP identity is
        recovered from the ``tool_call`` frame, which only the full extractor reads, and
        a call whose identity cannot be established is refused on every answering site,
        the auto-approve one included.
        """
        return (
            bool(self._spec_denied_tools)
            or self._enforces_zero_tool_ban
            or self.backend in ACP_BACKENDS_META_IDENTITY
            or self._is_copilot
        )

    @property
    def _is_claude(self) -> bool:
        return self.backend == ACP_BACKEND_CLAUDE

    @property
    def _is_opencode(self) -> bool:
        return self.backend == ACP_BACKEND_OPENCODE

    @property
    def _is_pi(self) -> bool:
        return self.backend == ACP_BACKEND_PI

    @property
    def _is_goose(self) -> bool:
        return self.backend == ACP_BACKEND_GOOSE

    @property
    def _is_deepseek(self) -> bool:
        return self.backend == ACP_BACKEND_DEEPSEEK

    @property
    def _is_copilot(self) -> bool:
        return self.backend == ACP_BACKEND_COPILOT

    @property
    def _is_strands(self) -> bool:
        return self.backend == ACP_BACKEND_STRANDS

    @property
    def _model_registry_namespace(self) -> str:
        """The model_registry namespace key for this backend (``claude_code`` /
        ``acp``). A registry index selector, NOT a provider-identity check — see
        agent_sdk.provider_identity note 3. Callers fold the wire model id and
        seed the allowlist against the right index for whichever backend is a
        member of ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION``."""
        return model_registry_namespace(self.backend)

    @property
    def _uses_advertised_model_selection(self) -> bool:
        """True when this backend sources its wire model id / seed from the
        provider's advertised list (see ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION``)."""
        return self.backend in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION

    @property
    def _seeds_local_settings(self) -> bool:
        """True when this backend seeds (and must re-seed) a per-session settings
        file (see ``ACP_BACKENDS_SEED_LOCAL_SETTINGS``)."""
        return self.backend in ACP_BACKENDS_SEED_LOCAL_SETTINGS

    @property
    def _is_kiro(self) -> bool:
        """True when this client drives kiro-cli (the AcpClient default).

        AcpClient serves kiro-cli, claude-agent-acp and the dormant codex seam, so
        this is the positive spelling for sites that would otherwise read
        ``not self._is_claude`` (harness-parity H5). KAS runs on AcpRuntime, not
        AcpClient, so it never reaches this property.
        """
        return self.backend == ACP_BACKEND_KIRO

    @property
    def member_dispatch_mounted(self) -> bool | None:
        """Whether this session's array carries the member session-control entry.

        ``None`` until :meth:`_append_member_dispatch_server` has composed an
        array for this client (no evidence yet); after that, the answer that
        composition gave -- False on every withhold, on a non-member session and
        on a backend outside ``ACP_BACKENDS_MEMBER_DISPATCH``.
        """
        return getattr(self, "_member_dispatch_mounted", None)

    # Prove and hand back Crew's ownership of a claude settings seed on disk: ``kiro_crew.acp.client_seed_ownership``.
    _claude_local_settings_path = _client_seed_ownership._claude_local_settings_path
    _claude_settings_is_still_ours = _client_seed_ownership._claude_settings_is_still_ours
    _settings_path_bytes = staticmethod(_client_seed_ownership._settings_path_bytes)
    _expected_settings_fingerprint = _client_seed_ownership._expected_settings_fingerprint
    _withdraw_shared_reader_lease = _client_seed_ownership._withdraw_shared_reader_lease
    _rename_aside_noreplace = staticmethod(_client_seed_ownership._rename_aside_noreplace)
    _copy_aside_back_without_clobber = staticmethod(
        _client_seed_ownership._copy_aside_back_without_clobber
    )
    _discard_claude_settings_seed = _client_seed_ownership._discard_claude_settings_seed
    _hand_back_unrevoked_claim = _client_seed_ownership._hand_back_unrevoked_claim
    _settle_claude_settings_seed = _client_seed_ownership._settle_claude_settings_seed
    release_settings_seed_claim = _client_seed_ownership.release_settings_seed_claim
    _foreign_settings_owner = _client_seed_ownership._foreign_settings_owner
    _foreign_settings_is_crew_seed = _client_seed_ownership._foreign_settings_is_crew_seed
    settings_withheld_cause = _client_seed_ownership.settings_withheld_cause
    _settle_claude_settings_seed_locked = _client_seed_ownership._settle_claude_settings_seed_locked

    @staticmethod
    def _settings_path_holds(path: Path, expectation: tuple[int, str] | None) -> bool:
        """Whether *path* still holds exactly the ``(size, sha256)`` in *expectation*.

        Split out from :meth:`_claude_settings_is_still_ours` so the teardown
        transaction can be a pure function of arguments captured before it starts.
        It runs in a thread while the client clears its instance flags, so a version
        that re-read ``self`` could decide ownership against state that changed
        underneath it. ``None`` means Crew has no claim to check, which reads as
        "not ours".
        """
        return AcpClient._settings_path_bytes(path, expectation) is not None

    @staticmethod
    def _claim_pathname_if_ours(
        path: Path, expectation: tuple[int, str] | None
    ) -> tuple[Path, tuple[int, int]] | None:
        """Atomically move *path* aside into a fresh name; return it IFF it is Crew's.

        Closes the window between an ownership check and the delete or overwrite that
        acts on it. ``_settings_path_holds`` verifies bytes by PATHNAME, but the delete
        or re-seed then mutates that same pathname a moment later -- and a user who
        atomically replaced the file in between would have their settings deleted or
        clobbered. ``os.replace`` captures whatever is at the pathname in ONE atomic
        step, so verifying the MOVED inode cannot then race: its bytes are fixed. A
        match is Crew's own seed (the caller deletes or replaces the moved file); a
        mismatch is a replacement that raced in, and it is put back untouched.

        The capture destination is a FRESH name created by ``mkstemp`` (``O_EXCL``) in
        the same directory, so it is a pathname this process just created and provably
        did NOT pre-exist. A fixed sibling name (e.g. ``<name>.crew-gc``) could name a
        file the project already owns, and ``os.replace`` onto it would clobber that
        file atomically -- relocating the very data loss this helper prevents one
        pathname over. ``os.replace`` onto our own fresh temp destroys only the empty
        temp we just made.

        ``(aside, identity)`` -- the moved file and ``(st_dev, st_ino)`` captured at
        the move, which every later restore and cleanup pins against. ``None``
        (nothing for the caller to mutate) when the path is gone, cannot be moved, or
        holds a file that is not Crew's. A crash between the move and the caller's
        follow-up leaves at most one such ``.crew-gc`` temp beside the target, which
        the next session's fresh seed ignores.
        """
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + ".", suffix=".crew-gc"
            )
        except OSError:
            return None
        tmp_st = os.fstat(fd)
        os.close(fd)
        aside = Path(tmp_name)
        try:
            os.replace(path, aside)
        except OSError:
            # Nothing to capture (path gone, or it cannot be moved): drop the empty
            # temp so a refusal never litters a stray file beside the target.
            with suppress(OSError):
                pinned_fs.unlink_verified_by_name(
                    path.parent, aside.name, (tmp_st.st_dev, tmp_st.st_ino)
                )
            return None
        try:
            moved = os.lstat(aside)
            moved_ident = (moved.st_dev, moved.st_ino)
        except OSError:
            # The moved entry vanished under us (a racing unlink): nothing left
            # to validate or restore. The empty temp name is gone with it.
            return None
        if AcpClient._settings_path_holds(aside, expectation):
            return aside, moved_ident
        # Not Crew's after all: a replacement raced in, or the user's own file --
        # a different regular file, a symlink the verifying open refuses to
        # follow, a file of any size. Put the moved entry back AS IT IS, without
        # clobbering whatever else may have taken the vacated name in the
        # meantime. No-clobber preserves the NEWER occupant and keeps the moved
        # entry recoverable beside it, where a replace-semantics restore would
        # silently destroy the newer one.
        AcpClient._restore_aside_without_clobber(aside, path, moved_ident)
        return None

    @property
    def _permission_surface_governed(self) -> bool:
        """Whether Crew controls this session's native permission surface.

        The precondition for delivering the ``mcpServers`` array, in either of
        its two shapes: this client authored ``settings.local.json`` itself
        (``_claude_settings_authored``), or the file's CURRENT on-disk bytes
        passed the sibling-seed validation
        (``_permission_surface_share_validated``). The durable reader lease is
        tracked separately in ``_claude_settings_shared`` because it survives a
        failed re-validation to keep the file pinned for a later re-earn, and the
        owner's un-revoked durable claim (``_claude_settings_claim_unrevoked``) is
        deliberately not read here either: it says a hand-back is still owed for
        a file already observed to be someone else's, never that Crew governs it.
        ``getattr`` on both governed flags because tests build clients without
        ``__init__``.

        A third shape governs without the file: the project owns it, and this
        session leaves it out of its setting sources
        (``_claude_local_settings_excluded``, see
        :meth:`_exclude_foreign_local_settings`). Its ``permissions.allow`` then
        never loads, so it cannot pre-approve a tool either.
        """
        return (
            getattr(self, "_claude_settings_authored", False)
            or getattr(self, "_permission_surface_share_validated", False)
            or getattr(self, "_claude_local_settings_excluded", False)
        )

    # Author an ``AcpClient`` session's claude ``settings.local.json`` seed: ``kiro_crew.acp.client_claude_seed``.
    _share_settings_seed_if_identical = _client_claude_seed._share_settings_seed_if_identical
    _share_sibling_settings_seed = _client_claude_seed._share_sibling_settings_seed
    _retract_reseed = _client_claude_seed._retract_reseed
    _log_declined_share = _client_claude_seed._log_declined_share
    _render_claude_settings_payload = _client_claude_seed._render_claude_settings_payload
    _write_claude_local_settings = _client_claude_seed._write_claude_local_settings
    _reseed_after_capture = _client_claude_seed._reseed_after_capture
    _seed_session_settings = _client_claude_seed._seed_session_settings

    @staticmethod
    def _restore_aside_without_clobber(aside: Path, path: Path, expect: tuple[int, int]) -> bool:
        """Put a moved-aside entry back under *path* without replacing a new occupant.

        A same-directory no-clobber rename (:meth:`_rename_aside_noreplace`), so a
        symlink, a directory or a file of any size the user placed at the settings
        path is restored exactly as it was captured -- no copy, no size cap, no read
        of its bytes -- and the aside name is consumed by the move. An occupant that
        arrived at the pathname while the entry was out is newer, so the rename
        refuses it and the moved entry stays recoverable beside it as ``.crew-gc``
        litter. Where no no-clobber rename primitive exists the restore falls back
        to the validated byte copy :func:`pinned_fs.put_back_no_clobber`, pinned to
        the moved inode *expect*; that path restores only a regular file.
        """
        try:
            if AcpClient._rename_aside_noreplace(aside, path):
                return True
        except FileExistsError:
            back: str | None = pinned_fs.PUT_BACK_NAME_TAKEN
        except OSError:
            logger.warning("could not restore %s; it is at %s", path, aside.name)
            return False
        else:
            back = AcpClient._copy_aside_back_without_clobber(aside, path, expect)
            if back is None:
                return True
        if back == pinned_fs.PUT_BACK_NAME_TAKEN:
            logger.info(
                "%s was recreated while Crew's seed was moved aside; leaving the new "
                "file in place (the moved seed remains at %s).",
                path,
                aside.name,
            )
            return False
        logger.warning("could not restore %s; it is at %s", path, aside.name)
        return False

    # The claude-agent-acp session contract an ``AcpClient`` keeps: ``kiro_crew.acp.client_claude_session``.
    _exclude_foreign_local_settings = _client_claude_session._exclude_foreign_local_settings
    _claude_adapter_below_setting_sources_floor = (
        _client_claude_session._claude_adapter_below_setting_sources_floor
    )
    _log_claude_adapter_below_floor = _client_claude_session._log_claude_adapter_below_floor
    _prepare_claude_side_read_denies = _client_claude_session._prepare_claude_side_read_denies
    _claude_session_meta = _client_claude_session._claude_session_meta
    _claude_session_excludes_local = _client_claude_session._claude_session_excludes_local
    _verify_claude_project_denies_unchanged = (
        _client_claude_session._verify_claude_project_denies_unchanged
    )
    _pin_claude_starting_mode = _client_claude_session._pin_claude_starting_mode

    async def confirm_member_projection(self) -> None:
        """Confirm the projection this session consumed. Blocking.

        Called from the adapter-only arm of ``_initialize_session``, so every
        session ``ensure_ready`` creates is judged before it returns, and again by
        the provider after it returns (a no-op for a session already confirmed). A
        session whose array was withheld, whose saved intent cannot be read, or whose
        saved intent is not the spec the array was built from is refused before its
        first prompt. A host that sends no array
        (kiro-cli, which receives its servers through ``--agent``) and a session with
        no saved intent are both no-ops -- and because the call sits on that arm and
        not on a step of the shared construction path, Kiro's startup gains no
        conditional, call or failure point in service of an adapter (harness-parity
        H13).

        Idempotent for the session it confirmed: a later start that re-enters on the
        same live session does not judge it again against a saved intent that may have
        moved since the session was created. State recorded by the confirmation is
        dropped with the session, so a NEW session is always judged afresh.
        """
        if not self.member_context or not self._session_id:
            return
        if self._confirmed_capability_session == self._session_id:
            return
        if (
            self.backend not in ACP_BACKENDS_MEMBER_CAPABILITIES
            or self.backend not in ACP_BACKENDS_SESSION_MCP_ARRAY
        ):
            return
        await asyncio.to_thread(self._confirm_member_projection, self._session_agent_spec)
        self._confirmed_capability_session = self._session_id

    def _confirm_member_projection(self, spec: dict[str, Any] | None) -> None:
        """Confirm the consumed projection, never a spec re-read after startup."""
        if not self.member_context:
            return
        from kiro_crew.agent_capabilities import CapabilityError
        from kiro_crew.agent_capabilities_consumed import confirm_consumed_projection

        try:
            gaps = confirm_consumed_projection(
                self._agent,
                spec,
                withheld=self._seeds_local_settings and not self._permission_surface_governed,
            )
        except CapabilityError as exc:
            raise AcpError(f"{exc.code}: saved spec projection could not be confirmed") from exc
        if gaps is None:
            return
        self._capability_projection_gaps = gaps
        self._loaded_capability_template = self._agent

    @property
    def loaded_capability_template(self) -> str:
        """Saved template whose projection was consumed by this live session."""
        if (
            self.backend in ACP_BACKENDS_MEMBER_CAPABILITIES
            and self.is_ready
            and self._is_process_alive()
        ):
            return self._loaded_capability_template
        return ""

    @property
    def capability_projection_gaps(self) -> tuple[str, ...]:
        return self._capability_projection_gaps

    @property
    def is_ready(self) -> bool:
        return self._process is not None and self._session_id is not None

    # Own an ``AcpClient`` child process after it is spawned: ``kiro_crew.acp.client_lifecycle``.
    _is_process_alive = _client_lifecycle._is_process_alive
    is_responsive = _client_lifecycle.is_responsive
    touch_activity = _client_lifecycle.touch_activity
    set_resume_session_id = _client_lifecycle.set_resume_session_id
    set_fork_request = _client_lifecycle.set_fork_request
    rekey = _client_lifecycle.rekey
    reclaim = _client_lifecycle.reclaim
    _discard_sandbox_cleanup = _client_lifecycle._discard_sandbox_cleanup
    _discard_bound_workspace = _client_lifecycle._discard_bound_workspace
    _session_work_dir = _client_lifecycle._session_work_dir
    _cleanup_failed_live_spawn = _client_lifecycle._cleanup_failed_live_spawn
    _to_thread_guarding_sandbox = _client_lifecycle._to_thread_guarding_sandbox
    _drain_stderr = _client_lifecycle._drain_stderr
    _snapshot_process_tree = _client_lifecycle._snapshot_process_tree
    refresh_process_tree = _client_lifecycle.refresh_process_tree
    _kill_process = _client_lifecycle._kill_process
    _reset_state = _client_lifecycle._reset_state
    _classify_failed_start = _client_lifecycle._classify_failed_start
    _settle_stderr = _client_lifecycle._settle_stderr
    _sandbox_init_failure = _client_lifecycle._sandbox_init_failure
    _delegate_launch_failure = _client_lifecycle._delegate_launch_failure
    _registration_throttle_line = _client_lifecycle._registration_throttle_line
    shutdown = _client_lifecycle.shutdown
    _retire_liveness_state = _client_lifecycle._retire_liveness_state

    @property
    def work_scratch_dir(self) -> Path | None:
        """The session tree's work directory this process exposes as ``$KIROCREW_SCRATCH``.

        The inherited directory when this client was spawned into an existing
        tree, else its own allocation; ``None`` before spawn or when allocation
        failed. Handed as ``shared_scratch`` to every spawn made on behalf of
        this session (see ``session_allocation._collect_parent_runtime_kwargs``).
        """
        return self._shared_scratch or self._scratch_dir

    def is_process_alive(self) -> bool:
        """True if the underlying process exists and has not exited."""
        return self._is_process_alive()

    @property
    def process_tree_confirmed_dead(self) -> bool:
        """Whether shutdown confirmed the root and every tracked child exited."""
        return self._process_tree_confirmed_dead is True

    @property
    def process_instance(self) -> str:
        """Identity of the CURRENT child process instance (``""`` when none).

        A fresh random id per spawn. This — not the ACP session id — is what a
        resource minted by the child must be compared against later: a resume
        carries the SAME session id onto a NEW process (``ensure_ready``'s
        session/load path re-sets ``_session_id`` from ``resume_sid``), so a
        session-id comparison fails open exactly when the minting process is
        gone. Liveness is a separate question: this names which spawn, while
        :meth:`is_process_alive` answers whether it still runs.
        """
        return self._process_instance if self._process is not None else ""

    @property
    def exit_code(self) -> int | None:
        """Return the process exit code, or None if still running / never started."""
        return self._process.returncode if self._process else None

    @property
    def resumed(self) -> bool:
        """True if the last session was restored via session/load."""
        return self._resumed

    @property
    def session_identity_token(self) -> str:
        """This session's per-session identity token, or ``""``.

        The uniform name the shared per-turn publisher reads
        (``messaging.identity._publish_session_token``), for the same reason
        :meth:`reclaim` is uniform: the publisher must stay backend-agnostic, and
        the two ACP providers keep the token in different places — this one on the
        client, the shared-runtime one on the session handle. A provider without
        this attribute is simply not asked.
        """
        return self._stub_session_token

    # Select, verify and apply an ``AcpClient`` session's model: ``kiro_crew.acp.client_model_selection``.
    set_model = _client_model_selection.set_model
    _capture_available_models = _client_model_selection._capture_available_models
    _persist_advertised_models_if_changed = (
        _client_model_selection._persist_advertised_models_if_changed
    )
    available_models = _client_model_selection.available_models
    _advertised_model_ids = _client_model_selection._advertised_model_ids
    _model_is_unusable = _client_model_selection._model_is_unusable
    refresh_available_models = _client_model_selection.refresh_available_models
    _entitlement_probe_client = _client_model_selection._entitlement_probe_client
    _probe_advertised_models = _client_model_selection._probe_advertised_models
    _entitlement_probe_answer = _client_model_selection._entitlement_probe_answer
    _model_config_candidates = staticmethod(_client_model_selection._model_config_candidates)
    _push_model_config_option = _client_model_selection._push_model_config_option
    _ensure_served_default = _client_model_selection._ensure_served_default
    _note_agent_switched = _client_model_selection._note_agent_switched
    _judge_activated_model = _client_model_selection._judge_activated_model
    _reassert_adapter_resolved_model = _client_model_selection._reassert_adapter_resolved_model
    _apply_startup_model = _client_model_selection._apply_startup_model
    set_config_option = _client_model_selection.set_config_option
    _store_session_config = _client_model_selection._store_session_config
    _handle_config_option_update = _client_model_selection._handle_config_option_update
    _sync_effort_levels = _client_model_selection._sync_effort_levels
    supports_config_option = _client_model_selection.supports_config_option
    get_valid_effort_levels = _client_model_selection.get_valid_effort_levels

    # Answer an ``AcpClient`` session's permission requests and enforce its tool restrictions: ``kiro_crew.acp.client_permissions``.
    _apply_session_permission_routing = _client_permissions._apply_session_permission_routing
    _verify_goose_routing = _client_permissions._verify_goose_routing
    approve_tool = _client_permissions.approve_tool
    _note_pi_gate_denied = _client_permissions._note_pi_gate_denied
    reject_tool = _client_permissions.reject_tool
    _handle_permission = _client_permissions._handle_permission
    _refuse_identity_drift = _client_permissions._refuse_identity_drift
    _foreign_mcp_identity = _client_permissions._foreign_mcp_identity
    _unclassified_tool_call = _client_permissions._unclassified_tool_call
    _tripwire_spec_disabled_tool = _client_permissions._tripwire_spec_disabled_tool
    _note_pi_adapter_version = _client_permissions._note_pi_adapter_version
    _note_goose_version = _client_permissions._note_goose_version
    _refuse_unverified_copilot_version = _client_copilot._refuse_unverified_copilot_version
    _tripwire_goose_mode = _client_permissions._tripwire_goose_mode
    _tripwire_session_config = _client_permissions._tripwire_session_config
    _seed_copilot_edit_targets = _client_copilot._seed_copilot_edit_targets
    _restore_copilot_edit_seed = _client_copilot._restore_copilot_edit_seed
    _confirm_copilot_mcp_identity = _client_copilot._confirm_copilot_mcp_identity
    _tripwire_permission_state = _client_permissions._tripwire_permission_state
    _drop_superseded_config_frames = _client_permissions._drop_superseded_config_frames
    _refuse_session_config_drift = _client_permissions._refuse_session_config_drift
    _tripwire_pi_gate = _client_permissions._tripwire_pi_gate
    # Answer an MCP server's elicitation on this client: ``kiro_crew.acp.client_elicitation``.
    _handle_elicitation_request = _client_elicitation._handle_elicitation_request
    _audit_elicitation_decision = _client_elicitation._audit_elicitation_decision
    _decline_elicitation = _client_elicitation._decline_elicitation
    answer_elicitation = _client_elicitation.answer_elicitation
    cancel_pending_elicitations = _client_elicitation.cancel_pending_elicitations
    _audit_spec_restriction = _client_permissions._audit_spec_restriction
    _deny_zero_tools = _client_permissions._deny_zero_tools
    _deny_spec_disabled_tool = _client_permissions._deny_spec_disabled_tool
    _reject_unknown_server_request = _client_permissions._reject_unknown_server_request
    _note_pi_gate_asked = _client_permissions._note_pi_gate_asked
    _build_permission_event = _client_permissions._build_permission_event

    @property
    def acp_config_options(self) -> list[dict]:
        """Config options reported by ACP (effort, model, mode selectors)."""
        return self._acp_config_options

    # JSON-RPC over an ``AcpClient`` child's stdio: ``kiro_crew.acp.client_transport``.
    _next_req_id = _client_transport._next_req_id
    _send_request = _client_transport._send_request
    _write_request_bounded = _client_transport._write_request_bounded
    _stdin_write_lock = _client_transport._stdin_write_lock
    _write_response_bounded = _client_transport._write_response_bounded
    _send_response = _client_transport._send_response
    _send_error = _client_transport._send_error
    _read_message = _client_transport._read_message
    _wait_for_response = _client_transport._wait_for_response
    _drain_notifications = _client_transport._drain_notifications
    _install_idle_reader = _client_transport._install_idle_reader
    _claiming_stdout = _client_transport._claiming_stdout
    _read_turn = _client_transport._read_turn
    _claim_stdout = _client_transport._claim_stdout
    _release_stdout = _client_transport._release_stdout
    _stop_idle_reader = _client_transport._stop_idle_reader
    _cancel_idle_reader = _client_transport._cancel_idle_reader
    _agent_turn_pending = _client_transport._agent_turn_pending
    _maybe_start_idle_reader = _client_transport._maybe_start_idle_reader
    _idle_read_loop = _client_transport._idle_read_loop

    # Prepare an ``AcpClient`` spawn before the process exists: ``kiro_crew.acp.client_spawn_env``.
    _prepare_spawn_workspace = _client_spawn_env._prepare_spawn_workspace
    _agent_env_for_spawn = _client_spawn_env._agent_env_for_spawn
    _apply_session_identity_env = _client_spawn_env._apply_session_identity_env
    _resolve_spawn_agent_argv = _client_spawn_env._resolve_spawn_agent_argv

    # Start an ``AcpClient`` session: spawn the child, run the initialize handshake, and create, load or fork the session: ``kiro_crew.acp.client_start``.
    _spawn = _client_start._spawn
    _new_session_following_substitution = _client_start._new_session_following_substitution
    _initialize_params = _client_start._initialize_params
    _fork_session = _client_start._fork_session
    _initialize_session = _client_start._initialize_session
    ensure_ready = _client_start.ensure_ready
    # Run an ``AcpClient`` prompt turn and dispatch the frames it reads: ``kiro_crew.acp.client_turn``.
    _process_message = _client_turn._process_message
    _defer_opencode_shell_permission = _client_turn._defer_opencode_shell_permission
    _prompt_loop = _client_turn._prompt_loop
    _consult_liveness_model_wait = _client_turn._consult_liveness_model_wait
    send_message = _client_turn.send_message
    send_message_stream = _client_turn.send_message_stream
    stream_events = _client_turn.stream_events
    stream_unsolicited = _client_turn.stream_unsolicited
    _dispatch_events = _client_turn._dispatch_events
    _send_prompt = _client_turn._send_prompt
    _read_prompt_response = _client_turn._read_prompt_response

    # Read Claude Code's native subagents on this session: ``kiro_crew.acp.client_native_children``.
    _native_children_declared = _client_native_children._native_children_declared
    _reset_native_child_turn = _client_native_children._reset_native_child_turn
    _native_child_roster = _client_native_children._native_child_roster
    _note_subagent_update = _client_native_children._note_subagent_update
    _note_session_message = _client_native_children._note_session_message
    _native_child_of = _client_native_children._native_child_of
    _native_child_events = _client_native_children._native_child_events
    # Their redaction before the turn yields them, and the flush of the text it
    # holds for a turn cut short: ``kiro_crew.acp.client_turn``.
    _redact_native_child_events = _client_turn._redact_native_child_events
    flush_native_child_text = _client_turn.flush_native_child_text

    # Control an ``AcpClient`` turn from outside it: ``kiro_crew.acp.client_turn_control``.
    command_result = _client_turn_control.command_result
    send_command = _client_turn_control.send_command
    stream_command = _client_turn_control.stream_command
    cancel_session = _client_turn_control.cancel_session
    _audit_cancelled_approvals = _client_turn_control._audit_cancelled_approvals
    steer = _client_turn_control.steer
    _steering_request_ready = _client_turn_control._steering_request_ready
    _steering_waiters = _client_turn_control._steering_waiters
    _unanswered_steering_ids = _client_turn_control._unanswered_steering_ids
    _settle_steering_answer = _client_turn_control._settle_steering_answer
    _steer_via_steering_request = _client_turn_control._steer_via_steering_request
    _await_steering_answer = _client_turn_control._await_steering_answer
    _release_accepted_steers = _client_turn_control._release_accepted_steers
    _steer_via_request = _client_turn_control._steer_via_request
    _steer_via_concurrent_prompt = _client_turn_control._steer_via_concurrent_prompt
    _dispatch_prompt_events = _client_turn_control._dispatch_prompt_events
    _close_steer_turn = _client_turn_control._close_steer_turn
    _add_orphan_steers = _client_turn_control._add_orphan_steers
    _settle_concurrent_steers = _client_turn_control._settle_concurrent_steers
    _read_steer_answers = _client_turn_control._read_steer_answers
    _write_steer_cancel = _client_turn_control._write_steer_cancel
    _settle_orphan_steers = _client_turn_control._settle_orphan_steers
    _note_active_run = _client_turn_control._note_active_run
    _settle_steer_answer = _client_turn_control._settle_steer_answer
    _steer_echo_event = _client_turn_control._steer_echo_event
    turn_finished_cleanly = _client_turn_control.turn_finished_cleanly
    wait_turn_done = _client_turn_control.wait_turn_done
    _note_background_launch = _client_turn_control._note_background_launch
    background_launch = _client_turn_control.background_launch
    has_active_turn = _client_turn_control.has_active_turn
    has_unfinished_turn = _client_turn_control.has_unfinished_turn

    # The prompt request whose read loop takes ``_session/steering`` steers right
    # now, or None: set when a prompt turn's dispatch starts, cleared when its
    # loop ends or its terminal is handled.
    _steering_turn_req: int | None = None
    # Whether the adapter advertised ``_meta.steering.supported`` at ``initialize``.
    _steering_advertised: bool = False
    # The whole ``initialize`` result, set by the handshake; read through
    # :attr:`initialize_result`, which answers ``{}`` before it.
    _initialize_result: dict
    # True while the turn's dispatch is parked on a permission event its consumer
    # has not answered: the answer to a steer sent then would not be read until
    # the approval is, past the steer's own wait.
    _parked_on_permission: bool = False
    # (turn request id, ``<user_message>``-wrapped text) for each steer accepted
    # into the running turn and not yet released by its terminal.
    _steers_accepted: list[tuple[int, str]]

    # Monotonic stamp of the last steer handed to the backend, 0.0 when never
    # steered. Mirrors AcpSessionHandle.last_steer_monotonic — the dashboard's
    # keepalive route reads whichever of the two backs the live session.
    _last_steer_monotonic: float = 0.0

    @property
    def last_steer_monotonic(self) -> float:
        """Monotonic time of the last steer written to the backend (0.0 if none)."""
        return self._last_steer_monotonic

    @property
    def supports_steer(self) -> bool:
        """True when the backend takes a user's mid-turn message.

        Membership in ``ACP_BACKENDS_STEER`` (kiro-cli's ``_session/steer``), in
        ``ACP_BACKENDS_STEERING_REQUEST``, or in ``ACP_BACKENDS_STEER_CONCURRENT_PROMPT``
        (a second ``session/prompt``), harness-parity H6, so a harness added later
        does not inherit any of them from ``not _is_claude``. A member of
        ``ACP_BACKENDS_STEERING_REQUEST`` takes its own request where this client
        speaks one (``_CLIENT_STEERING_REQUEST_METHODS``: goose); otherwise it also
        needs its adapter to have advertised ``_session/steering`` at the handshake
        and to be at or above its floor, and before the handshake it reads False.
        """
        if (
            self.backend in ACP_BACKENDS_STEER
            or self.backend in ACP_BACKENDS_STEER_CONCURRENT_PROMPT
            or self._speaks_steering_request
        ):
            return True
        return self.backend in ACP_BACKENDS_STEERING_REQUEST and self._steering_request_ready()

    @property
    def supports_refusal_steer(self) -> bool:
        """True when a deny notice steered into the refused turn reaches the model.

        Only ``ACP_BACKENDS_STEER``: a steering-request member's steer is reported
        consumed only at a clean end (``_session/steering``) or on its echo
        (goose), so it is not a deny-notice channel, and a harness that takes a
        steer as a second ``session/prompt`` ends its turn on the denial the
        notice is about.
        """
        return self.backend in ACP_BACKENDS_STEER

    @property
    def steer_needs_loss_recovery(self) -> bool:
        """True when an accepted steer can still be dropped with its turn.

        A ``_session/steering`` steer (``ACP_BACKENDS_STEERING_REQUEST``) and a
        concurrent-prompt steer (``ACP_BACKENDS_STEER_CONCURRENT_PROMPT``) are
        reported consumed only when their turn ends cleanly, so a cancelled or
        failed turn leaves them for the caller to queue. goose discards a steer
        still queued when its run ends, so its accepted steer is reported
        consumed only on goose's echo, and the caller requeues it otherwise.
        Only the dashboard composer keeps such an entry; kiro-cli's
        ``_session/steer`` keeps every steer delivered into the turn.
        """
        return (
            self.backend in ACP_BACKENDS_STEERING_REQUEST
            or self.backend in ACP_BACKENDS_STEER_CONCURRENT_PROMPT
        )

    @property
    def _speaks_steering_request(self) -> bool:
        """Whether this client speaks the member's own steer request
        (``_CLIENT_STEERING_REQUEST_METHODS``) rather than ``_session/steering``."""
        return (
            self.backend in ACP_BACKENDS_STEERING_REQUEST
            and self.backend in _CLIENT_STEERING_REQUEST_METHODS
        )

    # Turn an ``AcpClient`` session's tool-call frames into events, and audit them: ``kiro_crew.acp.client_tool_events``.
    _extract_text_chunk = _client_tool_events._extract_text_chunk
    _maybe_audit_tool_call = _client_tool_events._maybe_audit_tool_call
    _maybe_audit_unasked_tool_call = _client_tool_events._maybe_audit_unasked_tool_call
    _note_asked_tool_call = _client_tool_events._note_asked_tool_call
    _maybe_fire_pre_tool_hooks = _client_tool_events._maybe_fire_pre_tool_hooks
    _maybe_fire_post_tool_hooks = _client_tool_events._maybe_fire_post_tool_hooks
    _emit_tool_interrupted_sel = _client_tool_events._emit_tool_interrupted_sel
    _track_tool_call = _client_tool_events._track_tool_call
    _extract_tool_event = _client_tool_events._extract_tool_event
    _extract_tool_call_update = _client_tool_events._extract_tool_call_update
    _recover_copilot_mcp_identity = _client_tool_events._recover_copilot_mcp_identity
    _extract_tool_call_refinement = _client_tool_events._extract_tool_call_refinement
    _read_new_tool_results = _client_tool_events._read_new_tool_results
    _read_new_tool_results_sync = _client_tool_events._read_new_tool_results_sync
    _jsonl_tool_results = staticmethod(_client_tool_events._jsonl_tool_results)
    # Fold an ``AcpClient`` session's usage, metadata and compaction frames into its turn stats: ``kiro_crew.acp.client_usage``.
    _track_usage_update = _client_usage._track_usage_update
    _track_prompt_usage = _client_usage._track_prompt_usage
    _backfill_context_window = _client_usage._backfill_context_window
    _track_metadata = _client_usage._track_metadata
    _handle_compaction_status = _client_usage._handle_compaction_status
    _claude_compaction_event = _client_usage._claude_compaction_event
    _codex_compaction_event = _client_usage._codex_compaction_event
    _settle_codex_compaction = _client_usage._settle_codex_compaction
    _settle_claude_compaction = _client_usage._settle_claude_compaction
    wait_for_compaction = _client_usage.wait_for_compaction
    _drain_post_compaction_metadata = _client_usage._drain_post_compaction_metadata
