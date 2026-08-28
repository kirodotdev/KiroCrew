"""ACP (Agent Client Protocol) types for kiro-cli JSON-RPC communication."""

from __future__ import annotations

import json
import re as _re
from dataclasses import dataclass, field
from typing import Any

# ── ACP Event Kinds ──

EVENT_TEXT_CHUNK = "text_chunk"
EVENT_THINKING_CHUNK = "thinking_chunk"
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_CALL_UPDATE = "tool_call_update"
EVENT_TOOL_RESULT = "tool_result"
EVENT_PERMISSION_REQUEST = "permission_request"
EVENT_COMPLETE = "complete"
EVENT_COMPACTION_STATUS = "compaction_status"
EVENT_CLEAR_STATUS = "clear_status"
EVENT_AGENT_SWITCHED = "agent_switched"
EVENT_MCP_OAUTH_REQUEST = "mcp_oauth_request"
# Agent's own task/TODO list snapshot, recovered from the `todo_list` tool's
# rawOutput. Not an ACP-native update kind — see KIRO_TOOL_TODO_LIST.
EVENT_TODO_UPDATE = "todo_update"
EVENT_MCP_SERVER_INITIALIZED = "mcp_server_initialized"
EVENT_MCP_SERVER_INIT_FAILURE = "mcp_server_init_failure"
EVENT_SUBAGENT_LIST = "subagent_list"
EVENT_SUBAGENT_ACTIVITY = "subagent_activity"
EVENT_STEER_QUEUED = "steer_queued"
EVENT_STEER_CONSUMED = "steer_consumed"
EVENT_STEER_CLEARED = "steer_cleared"

# ── ACP Protocol Methods ──

METHOD_INITIALIZE = "initialize"
METHOD_SESSION_NEW = "session/new"
METHOD_SET_MODEL = "session/set_model"
METHOD_SET_MODE = "session/set_mode"
METHOD_PROMPT = "session/prompt"
METHOD_CANCEL = "session/cancel"
METHOD_REQUEST_PERMISSION = "session/request_permission"
METHOD_SESSION_UPDATE = "session/update"
METHOD_METADATA = "_kiro.dev/metadata"
METHOD_COMMANDS_EXECUTE = "_kiro.dev/commands/execute"
METHOD_SESSION_LOAD = "session/load"
#: Mid-turn steer. kiro-cli and KAS (kiro-cli-fronted, plus KAS's own
#: ``steering_*`` lifecycle echoes) implement this extension. Spec adapters
#: do not — callers must read ``supports_steer`` and degrade to follow-up
#: rather than sending a method the adapter cannot answer.
METHOD_SESSION_STEER = "_session/steer"
# kiro-cli extension: evict a session from the multiplexed process, freeing its
# transcript/context + reaping its MCP children. Without this the shared
# kiro-cli process retains every session's state for its whole lifetime, so RSS
# grows without bound as sessions accumulate. Handler: acp_agent.rs -> Session
# ManagerRequestData::TerminateSession (self.sessions.remove + handle.shutdown).
METHOD_SESSION_TERMINATE = "_kiro.dev/session/terminate"
#: KAS's equivalent. Its extension namespace is ``_kiro/`` (no ``.dev``), and it
#: has no evict-only verb: this disposes the resident session AND removes its
#: persisted record. Both are wanted here — the disposal is the memory reclaim
#: ``terminate`` exists for, and the record is what would otherwise accumulate.
#: Takes the same ``{"sessionId": ...}`` params and is idempotent.
METHOD_KAS_SESSION_DELETE = "_kiro/session/delete"
METHOD_COMPACTION_STATUS = "_kiro.dev/compaction/status"
METHOD_CLEAR_STATUS = "_kiro.dev/clear/status"
METHOD_AGENT_SWITCHED = "_kiro.dev/agent/switched"
METHOD_MCP_OAUTH_REQUEST = "_kiro.dev/mcp/oauth_request"
METHOD_MCP_SERVER_INITIALIZED = "_kiro.dev/mcp/server_initialized"
METHOD_MCP_SERVER_INIT_FAILURE = "_kiro.dev/mcp/server_init_failure"
METHOD_SUBAGENT_LIST_UPDATE = "_kiro.dev/subagent/list_update"
METHOD_KIRO_SESSION_UPDATE = "_kiro.dev/session/update"
METHOD_SET_CONFIG_OPTION = "session/set_config_option"
#: ``configId`` under which KAS exposes the session model. KAS implements no
#: ``session/set_model``, so this is the only way to switch a model on it.
MODEL_CONFIG_ID = "model"

#: JSON-RPC 2.0 reserved error code for an unrecognized method.
JSONRPC_METHOD_NOT_FOUND = -32601

# kiro-cli exposes its task/TODO list as an ordinary tool call whose real name
# arrives in `_meta.kiro.toolName` (the visible `title` is a prose sentence like
# "Creating task list: …", so it is NOT a reliable discriminator). Note this is
# NOT the ACP `plan` session update: kiro-cli 2.14.0 never emits `plan`, so
# UPDATE_PLAN below stays inert and the TODO list is recovered from this tool's
# rawOutput instead.
KIRO_TOOL_TODO_LIST = "todo_list"
# Hard cap on tasks retained per slot. The list is agent-authored and reaches
# the browser on every reconnect, so it is bounded server-side.
TODO_TASKS_MAX = 200
# Per-task text cap — keeps one pathological entry from bloating every payload.
TODO_TEXT_MAX = 500

# Capabilities we advertise during `initialize`.
#
# `elicitation` is a deliberate forward-bet: kiro-cli 2.14.0 compiles the
# `elicitation/create` schema (form + url modes) and gates it on this
# capability, but does NOT yet route an MCP server's `elicitation/create` out
# over ACP — a stub MCP server issuing one gets back
# `-32601 method not found`. Declaring support costs nothing today and means
# the agent can start using the richer prompt the moment kiro-cli ships the
# bridge.
#
# `fs` and `terminal` stay false: KiroCrew does not serve the agent's file or
# terminal requests over ACP — the agent uses its own tools for that, and
# advertising them would invite requests we have no handler for.
ACP_CLIENT_CAPABILITIES: dict = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
    "elicitation": {"form": {}, "url": {}},
}

# ── ACP Backend Identifiers ──

ACP_BACKEND_CLAUDE = "claude"
ACP_BACKEND_KAS = "kas"
# OpenAI Codex through the standalone codex-acp adapter, authenticated by the
# ChatGPT-subscription OAuth that `codex login` persists under $CODEX_HOME. No
# API key is read or stored: the adapter reads its own credential file.
ACP_BACKEND_CODEX = "codex"
# goose through its own built-in `goose acp` server. Unlike the codex and claude
# adapters, goose DELEGATES filesystem reads/writes and terminal execution back to
# the ACP client rather than performing them in-process, and asks per tool call —
# so Kiro Crew's PreToolUse gate sees the operations themselves, not just a
# request to be told about them afterwards.
ACP_BACKEND_GOOSE = "goose"
# OpenCode through its own ``opencode acp`` server (binary distribution).
ACP_BACKEND_OPENCODE = "opencode"
# pi through the registry ``pi-acp`` adapter (npx / global ``pi-acp``).
ACP_BACKEND_PI = "pi"
# The kiro-cli backend is spelled as the empty string throughout, so name it
# rather than leaving every call site to infer it from "not claude".
ACP_BACKEND_KIRO = ""
# Membership gate for the ``acp_backend`` kwarg. An unrecognized value would
# otherwise fall through every ``_is_<backend>`` check and silently spawn
# kiro-cli, so provider construction rejects it instead.
ACP_BACKENDS_KNOWN = frozenset(
    {
        ACP_BACKEND_KIRO,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_KAS,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
    }
)
# What an operator may actually persist in ``agent.acp_backend``, which is a
# narrower question than what the code understands. The initial preview is
# deliberately limited to the backends agreed for rollout; goose, OpenCode, pi,
# and registry-only adapters remain described but withheld until basic
# end-to-end evidence exists. Config resolution degrades an unselectable value
# to the default, so a typo costs a log line rather than a gateway that will not
# start.
#
# Membership means an operator may persist the value. Routing is a separate
# axis on the descriptor: ``Routing.UNVERIFIED`` still refuses at session start
# unless the operator sets the one named opt-out. Collapsing those would make
# the picker look like a guarantee that the gate is armed.
#
# Each backend now resolves its OWN spawn argv — ``AcpClient._spawn`` dispatches on
# a positive backend id per adapter, with kiro remaining the trailing fall-through.
# Before that it branched only on ``_is_claude``, so selecting codex or goose
# launched kiro-cli, and an earlier revision of this comment claimed codex had
# "completed a real turn end to end through AcpClient" on the strength of it. That
# turn was kiro-cli answering, which is exactly why it succeeded on a host with no
# codex credential. Do not restore that claim from a passing turn alone — check
# which binary answered.
#
# What IS verified, per backend:
#   codex   session-config ``mode=read-only`` applied after session/new|load;
#           this blocks writes but does not permission-route passive reads.
#           The standard sandbox deliberately leaves credential homes readable,
#           so Codex remains withheld until reads are gated or those homes are
#           hidden at the OS boundary. The resolver still returns the real
#           codex-acp entry script for integration work.
#   claude  the adapter's own settings resolver reads the very path
#           ``claude.local_settings_path`` writes and merges it through the Claude
#           Agent SDK, and the mode Kiro Crew seeds de-escalates, so the SDK's
#           ``filterEscalatingDefaultMode`` cannot discard it. Read from the
#           installed adapter, not observed as a permission prompt — and the SDK
#           marks those functions ``@alpha``, so a release could move this.
#           WITHHELD: reset currently unlinks the whole seeded project settings
#           file, including unrelated operator-owned keys that predated the
#           session. It cannot be selectable until cleanup owns only its change.
#   goose   session/request_permission for privileged tools. File I/O stays
#           in-process because we do not advertise fs/*; permission still
#           applies. OpenCode and pi use the same permission-request routing.
ACP_BACKENDS_SELECTABLE = frozenset(
    {
        ACP_BACKEND_KIRO,
        ACP_BACKEND_KAS,
    }
)

# ── Capability membership (harness-parity H6, H7) ──
# Every capability a backend may claim is an OPT-IN set here, never a negation at
# the call site. ``not is_claude_backend`` reads correctly with two backends and
# then silently hands the capability to the third, so a harness that has never
# demonstrated the capability inherits it — and the operator who never opted into
# that harness is the one who finds out. Adding a member is a deliberate edit
# with evidence; inheriting a default is not a decision. See
# docs/system-specs/modules/harness-parity.md.

# Backends whose single process can host N concurrent ACP sessions (AcpRuntime
# demux) AND can persist a SHARED subagent session across teardown. KAS runs on
# AcpRuntime (multi-session), but its teardown maps to _kiro/session/delete,
# which removes the persisted session — so a shared subagent would strand
# spawn_continue (conversation_gone). KAS therefore opts in only once a
# keep-aware teardown lands (native subagent work); until then its subagents get
# dedicated sessions. claude-agent-acp runs through AcpClient (one process per
# session) and is not a member.
ACP_BACKENDS_SESSION_SHARING = frozenset({ACP_BACKEND_KIRO})

# Backends verified to implement the ``_session/steer`` extension. KAS is a
# member because (1) the default spawn is ``kiro-cli acp --agent-engine v3``,
# which is kiro-cli's ACP surface (the same method), and (2) KAS emits the
# matching lifecycle frames Crew already maps (``steering_queued`` /
# ``steering_injected`` / ``steering_cleared`` on ``session_info_update``).
# Spec adapters are not members and must not inherit this from a negation.
ACP_BACKENDS_STEER = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends carrying their OWN internal OS sandbox, which on macOS cannot nest
# inside Kiro Crew's seatbelt (kernel EPERM) — so ``sandbox.wrap_argv`` skips
# Crew's own layer for them. This is the one membership test that fails OPEN:
# claiming it for a harness with no internal sandbox hands isolation to a layer
# that never starts and leaves the agent process unconfined. Only kiro-cli
# qualifies; a Node or Python harness does not, however it is spawned.
#
# KAS is NOT a member even though Crew now spawns it as ``kiro-cli acp
# --agent-engine v3`` and the process on the end of the argv IS kiro-cli. The
# relay spawns the KAS server without an ``--sandbox`` argument, and KAS's
# sandbox factory resolves an absent config to its no-op backend, so no OS
# sandbox starts inside — adding KAS here would skip Crew's seatbelt in favour of
# a layer that does not exist. See :mod:`kiro_crew.acp.kas_transport`.
ACP_BACKENDS_INTERNAL_SANDBOX = frozenset({ACP_BACKEND_KIRO})

# Backends served by AcpRuntime + AcpSessionHandle — the kiro-agent family
# (kiro-cli and KAS) whose single process hosts N sessions via demux. The
# dormant claude-agent-acp seam runs one AcpClient per session and is NOT a
# member. Membership drives the shared runtime start path and the kiro-family
# spawn conventions: members read the cli.json effort/tool-search overlay and
# receive effort at spawn, whereas claude applies it via a live push after the
# session is ready. Stated as opt-in membership (harness-parity H5/H6) so the
# four sites that mean "kiro or kas" say so positively rather than as
# ``not is_claude_backend`` — an inference that silently captures every harness
# added later. This is a SUPERSET of ACP_BACKENDS_SESSION_SHARING: running on
# AcpRuntime is necessary for session sharing but not sufficient (KAS runs here
# yet is excluded from sharing until keep-aware teardown lands).
ACP_BACKENDS_ACP_RUNTIME = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends whose sign-in lives in kiro-cli's OWN identity store, so an external
# ``kiro-cli logout`` (or a switch to another account) invalidates a process that
# is already running. Membership is what authorizes retiring a live session's
# child when that store starts naming a different account: a harness
# authenticated some other way must not be recycled on a store it never reads.
# KAS is a member: it is spawned as ``kiro-cli acp --agent-engine v3
# --auth-method cli`` (see :mod:`kiro_crew.acp.kas_transport`), and that
# ``--auth-method cli`` is precisely the demonstration this set waits for — the
# relay resolves every access token from kiro-cli's own store, so a logout that
# invalidates the kiro backend invalidates a running KAS relay identically.
# Excluding it would let a KAS session keep serving turns on the previous
# account's credentials. Positive membership rather than "not claude"
# (harness-parity H5).
ACP_BACKENDS_KIRO_IDENTITY_STORE = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends that serve the ``"auto"`` model sentinel, i.e. resolve it server-side
# into a real model. ``"auto"`` is a kiro-namespace id, not a protocol concept:
# the kiro-agent family advertises it as a row in its own model list, while a
# spec adapter (claude advertises ``default``; codex advertises
# ``openai.gpt-5.6-sol``, …) has no such id and rejects it at the wire.
#
# Membership governs only the surfaces that must name a model BEFORE any live
# list is known -- the picker's cold-start and degraded fallbacks. Once a session
# has advertised, ``resolve_usable_model`` gates ``"auto"`` on the advertised set
# instead, which needs no per-backend knowledge and stays correct for a harness
# added later. Kept separate from ACP_BACKENDS_ACP_RUNTIME rather than folded
# into it: running on the shared runtime and serving a model id are independent
# claims, and a harness could plausibly do either without the other.
#
# Offering an unusable ``"auto"`` fails in the direction that costs the operator
# a turn: it renders as the only row on offer, so it gets picked, and the failure
# lands at the wire as a bare -32603 with no hint that the row was never real.
# Showing nothing is the honest degraded state (harness-parity H6).
ACP_BACKENDS_AUTO_MODEL = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends whose turns are billed against the operator's KIRO CREDIT PLAN — the
# used/limit pair `/api/sessions/usage` reports and the header capsule renders.
# The plan belongs to the signed-in Kiro account, and only the kiro-agent family
# draws on it: a spec adapter bills through its own vendor account entirely
# (claude-agent-acp reports a per-turn dollar figure, codex-acp tracks its own
# rate limits), so the credits neither move nor describe that session's spend.
#
# Membership is what gates the readout, and both directions of getting it wrong
# cost the operator something real. Showing the pill for a non-member states a
# balance nobody is drawing down, next to a harness whose actual spend is
# invisible — and the number then looks frozen, which reads as a broken counter
# rather than as the wrong account. Worse, populating it is not free: the
# fallback source is a BILLED `kiro-cli chat ... /usage` turn on a 30-second
# timer, so a harness that spends no credits would spend them to render a pill
# that describes something else. Stated positively (harness-parity H5/H6) rather
# than as "not a spec dialect": billing is a property of the account a harness
# authenticates to, not of the wire dialect it speaks, and a future kiro-billed
# adapter on the spec dialect would be silently excluded by that inference.
ACP_BACKENDS_KIRO_CREDITS = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# ── Provider labels ──
# The backend identity key persisted in the session map. It indexes three
# things, so every producer must agree on it: resume compatibility
# (detect_provider_switch), session-map persistence, and session-file cleanup
# routing. Defined here rather than in providers.acp because session.py needs
# the vocabulary and cannot import that module at module scope.
#
# An absent label means kiro-cli, which is the default backend.
PROVIDER_LABEL_DEFAULT = "acp"
PROVIDER_LABEL_CLAUDE = "claude_code"
PROVIDER_LABEL_KAS = "kas"
PROVIDER_LABEL_CODEX = "codex"
PROVIDER_LABEL_GOOSE = "goose"
PROVIDER_LABEL_OPENCODE = "opencode"
PROVIDER_LABEL_PI = "pi"

# Labels of backends that speak the public ACP spec rather than kiro's dialect.
# They read no Kiro Crew agent config, so anything kiro-cli would have loaded from
# the agent spec (steering resources, mapped skill globs) must be injected into
# the prompt instead. Keyed on the LABEL because the consumers are context
# builders that receive a provider_type string, not a live client.
SPEC_ADAPTER_PROVIDER_LABELS = frozenset(
    {
        PROVIDER_LABEL_CLAUDE,
        PROVIDER_LABEL_CODEX,
        PROVIDER_LABEL_GOOSE,
        PROVIDER_LABEL_OPENCODE,
        PROVIDER_LABEL_PI,
    }
)
REGISTRY_PROVIDER_LABEL_PREFIX = "acp:"


def is_spec_adapter_provider_label(label: str) -> bool:
    """Whether a persisted provider label belongs to a public-spec adapter."""
    return label in SPEC_ADAPTER_PROVIDER_LABELS or label.startswith(REGISTRY_PROVIDER_LABEL_PREFIX)


# Which label a persisted session is filed under, per backend. A TABLE rather than
# a branch per backend in providers/acp.py: the label decides where a session's
# transcript is looked for, so a backend that falls through to the default is
# filed as kiro and then pruned for want of a kiro transcript. Adding a backend
# here is one line; forgetting it was previously a silent data-loss bug that only
# the label-uniqueness test caught. ``ACP_BACKEND_KIRO`` is deliberately absent —
# its label IS the default.
PROVIDER_LABELS_BY_BACKEND = {
    ACP_BACKEND_CLAUDE: PROVIDER_LABEL_CLAUDE,
    ACP_BACKEND_KAS: PROVIDER_LABEL_KAS,
    ACP_BACKEND_CODEX: PROVIDER_LABEL_CODEX,
    ACP_BACKEND_GOOSE: PROVIDER_LABEL_GOOSE,
    ACP_BACKEND_OPENCODE: PROVIDER_LABEL_OPENCODE,
    ACP_BACKEND_PI: PROVIDER_LABEL_PI,
}

# KAS reads only fs.readTextFile / fs.writeTextFile / terminal from the top
# level of clientCapabilities; every other capability it honours lives under
# _meta.kiro. The ones there are CALLBACK capabilities — KAS calls back into the
# client to service them — and Kiro Crew implements none, so leaving them
# undeclared (= false) is correct rather than a gap. Only the settings channel
# is opened, because that is how a client selects KAS feature flags.
KAS_CLIENT_CAPABILITIES: dict = {
    **ACP_CLIENT_CAPABILITIES,
    "_meta": {"kiro": {"settings": {}}},
}

# Public-ACP-spec adapters (claude-agent-acp, codex-acp) get the same set with
# `elicitation` REMOVED, and it must stay removed until Kiro Crew serves
# `elicitation/create`.
#
# codex-acp gates MCP tool-call approvals on `clientCapabilities.elicitation`:
# declare it and the adapter delivers those approvals as `elicitation/create`
# instead of `session/request_permission`. Kiro Crew has no handler, so the
# frame is answered `-32601`, and codex-acp converts that error into
# `action: "cancel"` — every MCP tool call is then silently cancelled with no
# prompt and no error the user can see. Absent the capability, the adapter falls
# back to `session/request_permission`, which is also the only path that reaches
# Kiro Crew's PreToolUse gate.
ACP_CLIENT_CAPABILITIES_SPEC_ADAPTER: dict = {
    key: value for key, value in ACP_CLIENT_CAPABILITIES.items() if key != "elicitation"
}

# ── Claude backend permission modes ──
# Values an edition writes into a per-session settings.local.json
# ``permissions.defaultMode`` when it drives the dormant ``ACP_BACKEND_CLAUDE``
# seam. ``default`` = per-tool approval; ``auto`` = the SDK auto-accept mode
# (Auto-mode / permission-UI parity). Inert in the public core (kiro-cli only);
# defined here so the client's ``permission_mode`` kwarg and a companion share
# one canonical vocabulary rather than duplicating string literals.
CC_PERMISSION_MODE_DEFAULT = "default"
CC_PERMISSION_MODE_AUTO = "auto"

# ── ACP Session Update Types ──

UPDATE_USER_MESSAGE_CHUNK = "user_message_chunk"
UPDATE_AGENT_MESSAGE_CHUNK = "agent_message_chunk"
UPDATE_AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
UPDATE_TOOL_CALL = "tool_call"
UPDATE_TOOL_CALL_UPDATE = "tool_call_update"
UPDATE_PLAN = "plan"
UPDATE_AVAILABLE_COMMANDS = "available_commands_update"
UPDATE_CURRENT_MODE = "current_mode_update"
UPDATE_CONFIG_OPTION = "config_option_update"
UPDATE_SESSION_INFO = "session_info_update"
UPDATE_USAGE = "usage_update"

# Vendor-namespaced ``_meta`` key on a ``usage_update``: claude-agent-acp
# forwards the Claude Code SDK's plan rate-limit block under it verbatim, on a
# usage frame emitted whenever that state CHANGES (its `rate_limit_event`). The
# key carries its own vendor namespace, so reading it needs no backend gate —
# an adapter that does not send it simply has no such key, and a positive
# is_claude_backend branch here would buy nothing while adding a conditional to
# a path every harness shares (H2).
META_CLAUDE_RATE_LIMIT = "_claude/rateLimit"

# Updates we recognise but don't yet surface (plumbing-only). Listed here so the
# "unhandled session update" log doesn't fire for them.
KNOWN_SESSION_UPDATES = frozenset(
    {
        UPDATE_USER_MESSAGE_CHUNK,
        UPDATE_AGENT_MESSAGE_CHUNK,
        UPDATE_AGENT_THOUGHT_CHUNK,
        UPDATE_TOOL_CALL,
        UPDATE_TOOL_CALL_UPDATE,
        UPDATE_PLAN,
        UPDATE_AVAILABLE_COMMANDS,
        UPDATE_CURRENT_MODE,
        UPDATE_CONFIG_OPTION,
        UPDATE_SESSION_INFO,
        UPDATE_USAGE,
    }
)

# Reserved tool argument carrying the agent's own one-line reason for a call.
# It is what the dashboard's concise tool label ("simplified tool names") shows
# instead of the literal invocation, so a missed key silently degrades every
# pill back to raw command text. These are the CANONICAL spellings — our tool
# schemas declare the snake_case name and kiro-cli echoes some calls back in
# ``rawInput`` with it camelCased — but they are not the only ones on the wire:
# models paraphrase the name (``__purpose``, ``__thinking_purpose``, …). Read
# via ``_dispatch.extract_tool_purpose()``, which prefers these two and then
# falls back to ``_dispatch.is_tool_purpose_key()`` shape matching; never index
# one literal.
TOOL_PURPOSE_KEYS: tuple[str, ...] = ("__tool_use_purpose", "__toolUsePurpose")

# ── ACP Permission Outcomes ──

OUTCOME_SELECTED = "selected"
OUTCOME_CANCELLED = "cancelled"
OPTION_ALLOW_ONCE = "allow_once"
OPTION_ALLOW_ALWAYS = "allow_always"

# ── Stop Reasons ──

STOP_REASON_CANCELLED = "cancelled"
STOP_REASON_END_TURN = "end_turn"
# Model-side content refusal ("response declined by the model"). Non-retryable:
# retrying the same prompt hits the same refusal, so chat_runner surfaces an
# actionable message instead of churning the retry ladder.
STOP_REASON_REFUSAL = "refusal"
# Signalled by the ACP layer when a genuinely-wedged (stale) turn was probed via
# session/cancel and got no ack within the grace window — a confirmed wedge, not
# a done-but-missing-frame turn (which acks and completes normally). The
# dashboard routes this to reset+resume+continue-nudge auto-recovery.
STOP_REASON_STALE_RECOVER = "stale_recover"
# Signalled by the per-session watchdog when an in-flight tool was judged dead
# / stuck / UNKNOWN-past-budget and the session was cancelled. Kept in the
# "error:" family so callers without a dedicated branch fall back to the
# generic error handling; chat_runner routes it to a dedicated recovery
# (continue-nudge, NOT a verbatim re-run of the original message).
STOP_REASON_TOOL_STALL = "error: tool stall"
# Signalled by the ACP layer when automatic compaction reported `failed`
# and the backend then abandoned the turn (no prompt response, no
# end_turn) past the post-failure budget. Kept in the "error:" family so
# callers without a dedicated branch fall back to generic error handling;
# it deliberately triggers NO retry — the user-visible compaction notice
# already explains what happened, and this only releases the slot.
STOP_REASON_COMPACTION_FAILED = "error: compaction failed"

# ── Approval Modes ──

APPROVAL_AUTO = "auto"
APPROVAL_INTERACTIVE = "interactive"


@dataclass
class JsonRpcRequest:
    """Outbound JSON-RPC 2.0 request."""

    method: str
    params: dict[str, Any]
    id: int
    jsonrpc: str = "2.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "jsonrpc": self.jsonrpc,
            "id": self.id,
            "method": self.method,
            "params": self.params,
        }


@dataclass
class JsonRpcMessage:
    """Inbound JSON-RPC 2.0 message (response or notification)."""

    id: Any = None
    method: str | None = None
    result: Any = None
    error: Any = None
    params: Any = None
    #: Set by ``AcpRuntime._reader_loop`` when this frame carried no
    #: ``sessionId`` and so was fanned out to MORE THAN ONE registered session.
    #: Such a frame names no owner: at most one of the recipients produced it and
    #: nothing says which, so a consumer must not read it as its own activity.
    #: False for a routed frame, and False for a fanout to a lone session (which
    #: IS the sole owner). Not part of the wire format -- ``from_dict`` never
    #: sets it.
    fanout_no_owner: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JsonRpcMessage":
        """Build a JsonRpcMessage from a parsed JSON-RPC frame."""
        return cls(
            id=data.get("id"),
            method=data.get("method"),
            result=data.get("result"),
            error=data.get("error"),
            params=data.get("params"),
        )

    def is_response_for(self, req_id: int) -> bool:
        # A JSON-RPC *response* carries an id + result/error and NO method.
        # The id space for our outbound requests (prompt, initialize, ...) is
        # independent of the agent's inbound *request* id space (server→client
        # session/request_permission), so the two can collide on the same
        # integer.  Requiring method is None ensures an inbound permission
        # request whose id happens to equal the in-flight prompt's req_id is
        # NOT misread as that prompt's completion (which would end the turn
        # early and leave the real tool permission unanswered → stuck turn).
        return self.id == req_id and self.method is None

    def is_method(self, name: str) -> bool:
        return self.method == name


@dataclass
class TurnUsage:
    """Per-turn usage/billing for one completed agent turn.

    Carried on AcpEvent (EVENT_COMPLETE). Each provider fills the dimensions it
    bills in and leaves the rest at 0: claude_code/bedrock fill token counts +
    cost_usd, kiro (acp) fills credits. Consumers read whichever is non-zero.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    credits: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0


#: Rate-limit states an adapter may report, in ascending severity. Kept as a
#: tuple rather than an enum because the value is passed through to the wire and
#: the dashboard as-is; an unrecognised spelling is dropped rather than mapped,
#: since guessing which side of "rejected" a new state falls on would be a
#: security-shaped guess about whether the user can still send a turn.
RATE_LIMIT_STATES: tuple[str, ...] = ("allowed", "allowed_warning", "rejected")


@dataclass(frozen=True)
class AcpRateLimit:
    """Plan rate-limit state for the account behind the current session.

    Distinct from :class:`TurnUsage` on two axes, which is why it is not a field
    on it: this describes the ACCOUNT over a rolling window (so it outlives any
    one turn and must survive a turn boundary), and it arrives only when the
    state CHANGES rather than once per turn.

    Sourced from claude-agent-acp's ``_meta["_claude/rateLimit"]``, which
    forwards the Claude Code SDK's ``SDKRateLimitInfo`` verbatim. Only the four
    fields a consumer can act on are carried; the SDK's overage and
    credit-purchase flags describe a billing flow Kiro Crew does not drive, and
    inventing a UI for them from a field name would be a guess.
    """

    #: One of :data:`RATE_LIMIT_STATES`; "" when the adapter sent no usable state.
    status: str = ""
    #: The rolling window this reading describes ("five_hour", "seven_day",
    #: "seven_day_opus", …), verbatim from the adapter. "" when absent.
    limit_type: str = ""
    #: Percent of the window consumed, 0-100. -1.0 = not reported, which is
    #: distinct from 0.0 ("window untouched") — a consumer that renders 0% for
    #: an absent reading claims a fresh quota the adapter never confirmed.
    utilization: float = -1.0
    #: Unix epoch SECONDS at which the window resets; 0.0 = not reported. The
    #: SDK types this only as ``number`` and declares no unit, so
    #: ``parse_rate_limit`` normalizes by magnitude rather than trusting either
    #: reading — see that function.
    resets_at: float = 0.0

    def is_reported(self) -> bool:
        """True when the adapter supplied at least one usable field.

        The all-defaults instance is indistinguishable from "no rate-limit
        telemetry", so consumers gate on this rather than on truthiness of an
        individual field — ``status`` alone would drop a reading that carried
        only a utilization figure.
        """
        return bool(self.status or self.limit_type) or self.utilization >= 0.0

    def to_payload(self) -> dict[str, Any]:
        """Serialize for the dashboard, omitting every unreported field.

        Absent fields are LEFT OUT rather than sent as a sentinel: the frontend
        renders whatever rows it receives, so shipping ``utilization: -1``
        would put "-1%" on screen. The sentinel is an internal spelling of
        "unknown" and must not cross the wire.
        """
        out: dict[str, Any] = {}
        if self.status:
            out["status"] = self.status
        if self.limit_type:
            out["limit_type"] = self.limit_type
        if self.utilization >= 0.0:
            out["utilization"] = round(self.utilization, 1)
        if self.resets_at > 0.0:
            out["resets_at"] = self.resets_at
        return out


def _normalize_to_kebab(name: str) -> str:
    """Convert PascalCase/camelCase to kebab-case for consistent deny matching.

    ``DeleteStack`` → ``delete-stack``, ``send-command`` → ``send-command``
    (already kebab, unchanged). This ensures the security deny globs (which are
    authored in kebab-case, e.g. ``*delete-stack*``) match regardless of whether
    kiro-cli or the LLM sends the AWS API PascalCase name or the CLI kebab name.

    The transform is injective within the space of valid AWS operation names
    (single-word PascalCase identifiers), so it cannot wrongly DENY a benign op
    by colliding with a destructive one: AWS operation names are globally unique
    per service, and the kebab form of each is equally unique.
    """
    # Insert hyphen before uppercase runs: "DeleteStack" → "Delete-Stack" → "delete-stack"
    s = _re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    s = _re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", s)
    return s.lower()


def _command_from_tool_params(params: dict) -> str | None:
    """Recover the verifiable command string from a shell tool's params dict.

    Returns the raw command for the Bash-style ``{"command": ...}`` shape, a
    synthesized ``aws <service> <operation> …`` line for the structured
    kiro-cli ``use_aws`` shape, or None when neither shape is present (the
    caller then falls back to deny-by-default).

    The ``use_aws`` synthesis serializes ``parameters`` / ``positional_args``
    into the tail so the security gate scans the FULL payload — a shell
    command smuggled inside ``ssm send-command`` parameters (e.g.
    ``{"commands": ["cat ~/.aws/credentials"]}``) is visible to
    ``is_sensitive_bash_command`` / ``audit_bash_exfiltration``, and
    destructive operations (``delete-stack``) match the built-in deny globs.
    Both fields come from the structured tool call kiro-cli executes, not
    from any LLM-authored display text, so they are trustworthy inputs for
    the gate (unlike ``title``/``description``).

    Security hardening (2026-08-05):
    - **Casing normalization**: ``operation_name`` is normalized from
      PascalCase/camelCase to kebab-case before synthesis, so the deny globs
      (authored in kebab) match regardless of the casing kiro-cli or the LLM
      sends. Without this, ``DeleteStack`` silently bypasses ``*delete-stack*``.
    - **Whitespace fail-closed**: ``service_name`` and ``operation_name``
      containing whitespace return None (deny-by-default) rather than
      synthesizing a multi-token string that could confuse downstream parsers.
    - **Best-effort caveat**: the serialized ``parameters`` tail uses
      ``json.dumps`` whose escaping (``\\"``, ``\\\\``) may render embedded
      payloads in a form the shell-text matchers were not authored for. This is
      acceptable for a single-user tool but is not a complete smuggling defense.
    """
    cmd = params.get("command")
    if isinstance(cmd, str) and cmd:
        return cmd
    service = params.get("service_name")
    operation = params.get("operation_name")
    if isinstance(service, str) and service and isinstance(operation, str) and operation:
        # Fail-closed: reject tokens containing whitespace — a multi-token
        # service_name or operation_name could confuse regex-based deny rules.
        if _re.search(r"\s", service) or _re.search(r"\s", operation):
            return None
        # Normalize operation_name to kebab-case so deny globs match regardless
        # of input casing (PascalCase API names like "DeleteStack" and CLI-style
        # "delete-stack" both produce "delete-stack"). service_name is left as-is
        # because AWS CLI services are already single lowercase tokens
        # ("cloudformation", "s3api", "dynamodb") and normalizing them would
        # incorrectly hyphenate ("cloud-formation") breaking deny regex matches.
        operation_norm = _normalize_to_kebab(operation)
        parts = ["aws", service, operation_norm]
        region = params.get("region")
        if isinstance(region, str) and region:
            parts.append(f"--region {region}")
        parameters = params.get("parameters")
        if isinstance(parameters, dict) and parameters:
            try:
                parts.append(json.dumps(parameters, sort_keys=True))
            except (TypeError, ValueError):
                parts.append(str(parameters))
        positional = params.get("positional_args")
        if isinstance(positional, list) and positional:
            parts.append(" ".join(str(p) for p in positional))
        return " ".join(parts)
    return None


@dataclass
class AcpEvent:
    """Structured event from kiro-cli ACP stream."""

    kind: str  # text_chunk, tool_call, permission_request, complete
    text: str = ""
    tool_call_id: str = ""
    title: str = ""
    tool_kind: str = ""
    tool_purpose: str = ""
    context_usage_pct: float = 0.0
    stop_reason: str = ""
    request_id: str | int = ""
    options: list[dict[str, str]] = field(default_factory=list)
    tool_input: str = ""
    #: True when the provider-facing tool input had secret/exfiltration bytes
    #: removed before it was placed in ``tool_input``.  This is provenance only:
    #: the original bytes never ride this display event.  Approval surfaces use
    #: it to refuse a durable command grant for a value the user could not see.
    tool_input_redacted: bool = False
    tool_output: str = ""
    tool_final: bool = False  # True when this tool_result is the final (status=completed) update
    usage: TurnUsage = field(default_factory=TurnUsage)
    raw_tool_params: dict | None = (
        None  # original tool params before diff conversion (for file-chip snapshots)
    )
    # MCP OAuth notification fields (EVENT_MCP_OAUTH_REQUEST):
    server_name: str = ""
    oauth_url: str = ""
    # Native subagent list (EVENT_SUBAGENT_LIST) — kiro-cli per-subagent state.
    subagents: list[dict[str, Any]] | None = None
    #: True when the frame behind this event named no owner and was fanned out to
    #: several sessions on one runtime (see ``JsonRpcMessage.fanout_no_owner``).
    #: A consumer must not read such an event as ITS OWN activity -- it is
    #: another tenant's traffic. Only the roster broadcast sets this today; the
    #: same event kind reached through a routed ``session/update`` (the KAS
    #: sub-agent lifecycle path) leaves it False, because that frame belongs to
    #: exactly one session.
    runtime_global: bool = False
    # Owning sub-agent session id (EVENT_SUBAGENT_ACTIVITY) — ties a tool call
    # to a specific native sub-agent card.
    sub_session_id: str = ""
    # Agent TODO-list snapshot (EVENT_TODO_UPDATE) — normalised
    # {description, tasks:[{id,text,completed}]}. Every todo_list command
    # returns the WHOLE list, so this is a full snapshot, never a delta.
    todo: dict[str, Any] | None = None
    # Provider-set canonical signal: True when this tool call is a shell/exec
    # command. Each provider maps its own vocabulary (ACP kind=="execute", CC
    # tool name "Bash") onto this one flag, so the dashboard validation layer
    # exempts shell commands from the tool-name length cap without hardcoding
    # provider-specific tool_kind literals (which silently re-break on every
    # engine migration / tool rename).
    is_shell: bool = False
    #: PROVENANCE flags for the child-fidelity gate (see child_low_fidelity).
    #: raw_params_trusted: raw_tool_params came from the tool_call cache (a
    #: frame this client parsed), not the permission payload's agent-authored
    #: inline fallback. shell_classified: is_shell reflects a resolved
    #: classification (cache hit), not the miss-default False.
    raw_params_trusted: bool = False
    shell_classified: bool = False
    #: mcp_identity_trusted: mcp_server_name/tool_name below were populated
    #: from a provenance-verified source — the origin-scoped tool_call caches
    #: (permission path) or ``_meta.kiro`` on the tool_call frame itself —
    #: never an inline/agent-authored fallback. Mirrors ``raw_params_trusted``:
    #: ``child_mcp_identity_trusted`` requires this flag IN ADDITION to
    #: non-empty identity fields, so a future population path that forgets it
    #: fails CLOSED (identity not counted as verified) instead of silently
    #: passing on non-emptiness alone.
    mcp_identity_trusted: bool = False
    # Canonical, NON-model-authored tool identity from ``_meta.kiro`` (see
    # ``_dispatch._kiro_tool_name``), or from a positively identified spec
    # adapter's ``mcp__<server>__<tool>`` title when ``_meta.kiro`` is absent
    # and ``kind`` is present and not execute.
    # kiro-cli ``title`` is LLM-authored prose — for shell tools
    # ``select_tool_title`` even prefers the model's ``description`` — so a
    # security gate MUST key on these, never on a bare title. ``mcp_server_name``
    # is populated ONLY for MCP-served tools (empty for built-ins/shell), so a
    # non-empty value is the trusted signal "a real MCP tool call" rather than a
    # forged shell result. Empty when neither ``_meta.kiro`` nor an explicitly
    # enabled spec-adapter title is present (fail-closed: callers that gate on
    # these get no match).
    tool_name: str = ""
    mcp_server_name: str = ""
    #: True when a spec-adapter MCP title matched more than one server in the
    #: exact session roster. The title encoding has no escaping, so no security
    #: identity can be recovered; permission consumers must hard-deny the call.
    mcp_identity_ambiguous: bool = False
    # Diff content block fields — authoritative before/after text from kiro-cli
    # for write tools. Used by chat_runner to derive the "before" snapshot
    # without a racy disk read (the write has already landed by the time the
    # event is processed). ``diff_old_text`` is None when no diff block was
    # present (fallback to disk read); empty string means "file was created"
    # (no previous content). ``diff_path`` is the path from the content block.
    diff_old_text: str | None = None
    diff_path: str = ""

    @property
    def shell_command(self) -> str | None:
        """The raw shell command for a shell tool call, else None.

        ``title`` for a shell tool may be an LLM-authored ``description``
        rather than the literal command (``select_tool_title`` prefers
        ``description``), so security gates must evaluate THIS instead of the
        title. Returns None for non-shell tools or when no command can be
        recovered — in the latter case the caller must fall back to
        deny-by-default (``is_shell`` with an unrecoverable command must NOT be
        gated on the untrusted title alone; see ``HookManager.on_tool_call``).

        The command is recovered from two shapes because different event kinds
        populate different fields:
        - ``raw_tool_params`` dict (tool_call / tool_call_update events), or
        - ``tool_input`` JSON string (permission_request events, where the ACP
          ``toolCall`` params are resolved into ``tool_input`` and
          ``raw_tool_params`` is NOT set — this is the dashboard's primary
          gate path, so the fallback is load-bearing, not a nicety).

        Two parameter shapes are recognized within each source:
        - Bash-style: a literal ``command`` string — returned verbatim.
        - Structured AWS CLI (kiro-cli ``use_aws``): ``service_name`` +
          ``operation_name`` (+ ``parameters``/``positional_args``). kiro-cli
          reports ``use_aws`` with the shell tool kind, so without this shape
          the deny-by-default backstop in ``HookManager.on_tool_call`` rejected
          EVERY ``use_aws`` call ("shell command could not be verified") — the
          v3.3.x regression that fully broke SSM for kiro-backend users. The
          structured fields are the ground truth of what executes (kiro-cli
          builds the CLI invocation from them, never from the display title),
          so synthesizing ``aws <service> <operation> …`` gives the gate real
          bytes to evaluate: destructive subcommands still match the built-in
          deny globs (``*delete-stack*``), and shell payloads embedded in
          parameters (e.g. ``ssm send-command`` ``commands``) are scanned by
          the sensitive-path / exfiltration checks via the serialized tail.
        """
        if not self.is_shell:
            return None
        if isinstance(self.raw_tool_params, dict):
            cmd = _command_from_tool_params(self.raw_tool_params)
            if cmd:
                return cmd
        # Fallback: recover the command from the tool_input JSON payload.
        if self.tool_input:
            try:
                parsed = json.loads(self.tool_input)
            except (ValueError, TypeError):
                return None
            if isinstance(parsed, dict):
                cmd = _command_from_tool_params(parsed)
                if cmd:
                    return cmd
        return None

    @property
    def child_low_fidelity(self) -> bool:
        """True for a backend-subagent event whose SECURITY context is absent.

        Gates every auto-approve path for runtime-routed child permission
        requests. ``tool_input`` alone is NOT fidelity: an edit refinement can
        cache a rendered diff string without ``raw_tool_params``, leaving the
        path-scope checks blind while a truthy ``tool_input`` suggests
        otherwise. Nor is a bare ``raw_tool_params`` dict: the permission
        frame's inline ``toolCall.input`` fallback is agent-authored, and a
        shell-cache MISS defaults ``is_shell`` to False — trusting either
        would let a benign inline dict on a shell tool masquerade as full
        context. Fidelity therefore requires PROVENANCE: params resolved from
        the tool_call cache (``raw_params_trusted``), a resolved shell
        classification (``shell_classified``), and — for a shell tool — a
        recoverable command string. Non-child events are never low-fidelity
        (their caches are slot-owned and complete by construction).
        """
        if not self.sub_session_id:
            return False
        if not self.raw_params_trusted or not isinstance(self.raw_tool_params, dict):
            return True
        if not self.shell_classified:
            return True
        if self.is_shell and not self.shell_command:
            return True
        return False

    @property
    def child_mcp_identity_trusted(self) -> bool:
        """True for a child MCP event whose IDENTITY is verified even when its
        arguments are not.

        ``child_low_fidelity`` conflates two independent provenances: the tool's
        identity and its arguments. A remote (HTTP) MCP server's ``tool_call``
        frame legitimately streams an empty ``rawInput``, so the params cache
        stays empty and every such child permission request is low-fidelity —
        yet the ``_meta.kiro`` server/tool identity from that same frame DID
        reach the caches and is non-model-authored. This property isolates that
        verified-identity half so UNCONDITIONAL grant paths — ones whose approve
        decision consumes no agent-authored event data (session trust-all,
        global YOLO, ``parent_policy=auto``, per-source auto-approve) — can
        honor the grant, while every content-matching path (trusted patterns,
        trust-reads, title-keyed ``auto_approve_tools``) stays gated on the
        composite ``child_low_fidelity``: for those the agent-authored title or
        inline params ARE the matched input, and a forged title must never
        satisfy them.

        Requirements, each fail-closed on its cache: a child origin
        (``sub_session_id``), a RESOLVED non-shell classification
        (``shell_classified`` and not ``is_shell`` — an unclassified event
        defaults to non-shell and must not pass as one; a shell tool's deny
        gates need the command bytes this event lacks), the canonical
        ``mcp_server_name`` + ``tool_name`` pair recovered from the tool_call
        cache (empty on a miss, and populated only for genuinely MCP-served
        tools — a host shell/builtin can never carry a server name), and the
        explicit ``mcp_identity_trusted`` provenance flag set by the trusted
        population sites — non-emptiness alone is NOT proof of provenance, so
        an identity pair written by any future inline/agent-authored fallback
        stays untrusted until that site earns the flag. A
        non-child event returns False: parents never need the split.
        """
        return bool(
            self.sub_session_id
            and self.shell_classified
            and not self.is_shell
            and self.mcp_identity_trusted
            and self.mcp_server_name
            and self.tool_name
        )

    @property
    def child_unconditional_grant_eligible(self) -> bool:
        """True when an UNCONDITIONAL grant path may honor this event.

        An unconditional grant is one whose approve decision consumes no
        agent-authored event data: session trust-all, global YOLO / the
        ``--approval yolo`` override, ``parent_policy=auto``, per-source
        auto-approve. Such a grant is eligible when the event has full
        fidelity (``not child_low_fidelity``) OR its canonical MCP identity is
        verified (``child_mcp_identity_trusted``) — for the latter only the
        ARGUMENTS remain unverified, which the grant never reads (the same
        blindness the interactive card has; the identity split changes WHO
        approves, not what any gate can scan). Content-MATCHING paths —
        trusted patterns, trust-reads, title-keyed ``auto_approve_tools``, the
        'reads' classification — must stay gated on the composite
        ``child_low_fidelity`` instead: the agent-authored title or inline
        params ARE their matched input, and a forged title must never satisfy
        them. Non-child events are always eligible (never low-fidelity).
        """
        return not self.child_low_fidelity or self.child_mcp_identity_trusted


@dataclass
class AcpPromptStats:
    """Stats from the last ACP prompt."""

    event_count: int = 0
    text_chunks: int = 0
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    context_pct: float = 0.0
    # Raw token counts from the adapter's usage_update {used, size}. context_pct
    # is derived as used/size*100, but the dashboard token TEXT must use the
    # real served window (size) — re-deriving it on the frontend from the model
    # id (e.g. assuming 1M for "[1m]") can disagree with the window the adapter
    # actually divided by, inflating the displayed "X / Y tokens". 0 = unknown.
    context_used_tokens: int = 0
    context_window_tokens: int = 0
    # True once a real ``usage_update {used, size}`` has set the token counts
    # above. When set, those counts (and the ``context_pct`` derived from them)
    # are AUTHORITATIVE: kiro's separately-streamed ``_kiro.dev/metadata``
    # ``contextUsagePercentage`` must NOT overwrite ``context_pct`` (it can
    # disagree with used/size — measuring a different window — which would make
    # the dashboard show a headline % inconsistent with the "used / total"
    # token text). Also gates the pct-only ``_backfill_context_window`` so a
    # registry-derived window never clobbers real served counts. Defaults False
    # and re-inits per turn, carried across turns alongside the counts.
    context_tokens_from_usage: bool = False
    # Per-turn billing credits summed from kiro's _kiro.dev/metadata
    # meteringUsage (unit="credit"). 0 for providers that bill in tokens.
    credits: float = 0.0
    # Cost in the adapter's own currency, taken from a `usage_update`'s optional
    # `cost` block. Distinct from `credits`, which is kiro's metering unit and
    # arrives by a different method (_kiro.dev/metadata) — an adapter fills one or
    # the other, never both, and a consumer reads whichever is non-zero.
    #
    # This is the CUMULATIVE session figure, not a turn delta: claude-agent-acp
    # sends `total_cost_usd`, matching how `used`/`size` on the same notification
    # are cumulative context rather than per-turn. Summing it across turns would
    # multiply the bill, so it is assigned, never accumulated.
    usage_cost: float = 0.0
    #: Currency for :attr:`usage_cost` as the adapter declared it (e.g. "USD").
    #: Never assumed — a bare number with an inferred currency is a wrong number.
    usage_cost_currency: str = ""
    # Plan rate-limit state for the ACCOUNT, from a usage_update's
    # _meta["_claude/rateLimit"]. None until an adapter reports one. Unlike the
    # context counts this is not a property of the transcript, so neither
    # compaction nor a model switch invalidates it — only a newer frame replaces
    # it, and `carry_over` keeps it across turns because the adapter emits it
    # ONLY on change: dropping it at a turn boundary would blank a live quota
    # reading until the user happened to cross another threshold.
    rate_limit: "AcpRateLimit | None" = None
    # True while ``context_pct`` reads 0.0 only because a compaction dropped the
    # counts and no fresh telemetry has re-derived them. Distinguishes "the
    # transcript is empty" from "the transcript's size is unknown" — the two are
    # indistinguishable by value, and a consumer that reads the second as the
    # first sees a session that just hit its context ceiling as brand new.
    # Cleared the moment a real percentage or usage_update lands.
    context_pct_unknown: bool = False

    def carry_over(self) -> "AcpPromptStats":
        """Return fresh per-turn stats carrying this turn's context state.

        Event/tool/credit counters are per-turn and start at zero; the context
        state describes the SESSION and must survive the re-init, or every turn
        boundary would re-report an empty context. ``rate_limit`` survives for a
        stronger reason: it describes the account, and its adapter sends it only
        when the state changes, so a dropped value is not re-reported next turn.
        """
        return AcpPromptStats(
            context_pct=self.context_pct,
            context_used_tokens=self.context_used_tokens,
            context_window_tokens=self.context_window_tokens,
            context_tokens_from_usage=self.context_tokens_from_usage,
            context_pct_unknown=self.context_pct_unknown,
            rate_limit=self.rate_limit,
        )

    def reset_context_state(self) -> None:
        """Drop ALL context state when the runtime is re-bound to a new session.

        The inverse commitment of :meth:`carry_over`: that method preserves the
        context fields because they describe the SESSION — which is exactly why
        they must NOT survive a warm-pool handoff, where the runtime outlives
        whatever it did before the re-bind. Stale stats handed to a new chat
        make ``check_context_usage`` fire compaction on an empty conversation
        (issue #2932).

        Everything returns to dataclass defaults, window included: a handoff
        may re-apply a different model post-claim, and a window measured before
        the re-bind has no claim to describe the next session.

        ``context_pct_unknown`` deliberately resets to ``False``, NOT ``True``:
        the claimed runtime serves a fresh, never-prompted ``session/new``, so
        "confirmed empty" is the accurate reading. Flagging it unknown would
        collide with the flag's existing meaning — "the backend compacted this
        session in place" — which the background-session recycle decision reads
        as a recycle-now signal (``pct == 0.0 and unknown``); a just-claimed
        provider must not match that predicate.

        ``rate_limit`` is deliberately NOT cleared — it is not context state.
        The re-bind swaps which conversation the runtime serves, not which
        account it bills, so the last known quota reading still describes the
        new session; and since the adapter re-sends it only on change, clearing
        it here would blank the reading for the rest of the process's life.
        """
        self.context_pct = 0.0
        self.context_used_tokens = 0
        self.context_window_tokens = 0
        self.context_tokens_from_usage = False
        self.context_pct_unknown = False

    def note_pct_reported(self) -> None:
        """Mark ``context_pct`` as backed by real telemetry.

        Called wherever a percentage or usage_update is applied, so a zero that
        follows a compaction stops reading as "unknown" once the backend says
        what the compacted transcript actually costs.
        """
        self.context_pct_unknown = False

    @staticmethod
    def sanitize_pct(value: object) -> float | None:
        """Coerce a raw context-usage percentage to a real [0, 100] float.

        Both the kiro-cli ``contextUsagePercentage`` and the KAS
        ``usagePercentage`` fields feed this. Returns ``None`` for a missing or
        unparseable value (the caller leaves the meter untouched). A malformed
        number (NaN, ±inf, or a huge finite like 1e308) is clamped — NaN via its
        self-inequality — so ``context_pct`` is always valid JSON and never
        overflows the downstream ``round(win * pct / 100)``.
        """
        if value is None:
            return None
        try:
            pct = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            # OverflowError: a JSON integer beyond float range — malformed
            # telemetry must degrade to "absent", never abort the active turn.
            return None
        return 0.0 if pct != pct else min(max(pct, 0.0), 100.0)

    def backfill_context_window(self, pct: float, model_id: str) -> None:
        """Derive window/used tokens from the model registry when only a
        percentage is available.

        kiro-cli 2.10+ metadata and KAS ``context_usage`` both give a percentage
        with no ``usage_update {used, size}``. Shared by the AcpClient and
        AcpSessionHandle paths (previously two verbatim copies) so both report
        the same context-meter token counts. No-op once a real usage_update has
        set authoritative counts. ``model_id`` is the caller's resolved id (the
        kiro-agent ``currentModelId``, else the user-picked alias). Resolves the
        window through ``model_registry.model_window`` (kiro-list cache >
        registry > heuristic) and only backfills a KNOWN window, leaving 0 for a
        genuinely-unknown model so the frontend's own authoritative window drives
        the meter. A real ``usage_update.size`` always wins. A surviving
        ``context_window_tokens`` (e.g. kept across a compaction reset — the
        model did not change) outranks the registry, since the served size can
        differ from the static entry.
        """
        if self.context_tokens_from_usage:
            return  # a real usage_update already set authoritative counts
        win = self.context_window_tokens
        if not win or win <= 0:
            if not model_id:
                return
            # Deferred import: model_registry is a leaf module, but importing it
            # at module scope would drag it into the very early types import.
            from kiro_crew import model_registry

            if not model_registry.has_known_window(model_id):
                return
            reg_win = model_registry.model_window(model_id)
            if not reg_win or reg_win <= 0:
                return
            win = int(reg_win)
            self.context_window_tokens = win
        # sanitize_pct already clamps live telemetry, but a caller may pass a raw
        # pct here; guard the multiply so a stray NaN/inf can never overflow.
        safe_pct = 0.0 if pct != pct else min(max(pct, 0.0), 100.0)
        self.context_used_tokens = round(win * safe_pct / 100.0)

    def reset_after_compaction(self) -> None:
        """Drop the usage counts after a successful compaction.

        The compacted transcript's true size is unknown until the next turn's
        telemetry reports it, and the pre-compaction counts no longer describe
        the session. Keeping them would re-broadcast a stale meter, and —
        worse — a stale ``context_tokens_from_usage=True`` gates
        ``_track_metadata`` / ``_backfill_context_window``, so even a fresh
        post-compaction metadata percentage could never correct it. The window
        is kept: the model did not change, so the served window still holds.

        The zeroed ``context_pct`` is flagged unknown, not empty: a consumer
        that recycles or compacts on a threshold would otherwise read a session
        sitting at its ceiling as freshly started and leave it in place, paying
        the backend's own auto-compaction over and over.
        """
        self.context_tokens_from_usage = False
        self.context_used_tokens = 0
        self.context_pct = 0.0
        self.context_pct_unknown = True

    def rebase_to_window(self, window_tokens: int) -> None:
        """Re-anchor the token stats to a new model's context window.

        Called after a mid-session ``session/set_model``: the previous model's
        window no longer describes the session, and ``context_tokens_from_usage``
        must drop so the next metadata percentage can re-derive against the new
        model (a stale True gates ``_backfill_context_window`` forever when the
        new model streams only ``contextUsagePercentage``). ``context_used_tokens``
        is kept — the transcript is unchanged and token counts are roughly
        model-independent — and ``context_pct`` is recomputed against the new
        window when it is known. Pass 0 for an unknown window: window AND pct
        zero out (the old model's pct must not ship in the reset broadcast),
        so downstream consumers fall back to their own model-derived value
        until the next turn's telemetry re-derives real numbers.
        """
        self.context_tokens_from_usage = False
        if window_tokens and window_tokens > 0:
            self.context_window_tokens = int(window_tokens)
            if self.context_used_tokens > 0:
                pct = self.context_used_tokens / window_tokens * 100.0
                self.context_pct = round(min(max(pct, 0.0), 100.0), 1)
        else:
            self.context_window_tokens = 0
            self.context_pct = 0.0
