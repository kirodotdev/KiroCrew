"""In-memory ACP session state. Never grants Crew tool permissions.

The event log and durable run store own persisted truth. These dataclasses
only store runtime projections and per-process locks/queues.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .agent_projection import AgentProjection
from .agent_runtime import AgentRegistry
from .agent_team import AgentTeamRegistry
from .mcp_client import McpManager
from .native_tools import NativeToolRuntime

@dataclass
class Session:
    session_id: str
    cwd: str
    model: str
    # Truth about the session selection surface. ``model`` remains the ACP wire
    # selection id; these fields make it explicit whether that id represents a
    # Tiga Type or a concrete model and where the last selection came from.
    selection_kind: str = "unknown"
    selection_source: str = ""
    selection_requested: str = ""
    effort: str = ""
    # Defaults advertised by a provider are not user intent. These flags let a
    # Type apply its reasoning policy unless the user explicitly changed the
    # corresponding control in this session.
    effort_user_set: bool = False
    thinking_user_set: bool = False
    # Deep-thinking on/off/auto is distinct from reasoning effort intensity.
    # Canonical values are "on", "off", "auto", or "" for provider default.
    thinking_mode: str = ""
    host_tool_approval_required: bool = False  # ephemeral; overrides saved-Agent allowlists
    # Ephemeral per-turn presentation policy. It is recomputed from the current
    # user conversation (or the configured manual override) and intentionally
    # is not persisted as session truth.
    reasoning_language_preference: str = "auto"
    ui_language_preference: str = "auto"
    presentation_language: str = ""
    presentation_language_source: str = ""
    tool_display_language: str = ""
    reasoning_language: str = ""
    reasoning_language_source: str = ""
    reasoning_language_instruction: str = ""
    response_language_instruction: str = ""
    language_instruction: str = ""
    # Client-owned ACP v1 filesystem roots. They are intentionally re-supplied
    # on load/resume rather than restored implicitly from the session record.
    additional_directories: tuple[str, ...] = ()
    agent: AgentProjection = field(default_factory=AgentProjection)
    # Crew-owned task/assignment correlation only. Projected from host/work_link
    # events; never used as an authorization grant or a model routing signal.
    host_work_link: dict[str, str] = field(default_factory=dict)
    # Provider-facing transcript projection. 0.1.3 persists the append-only
    # event log below as the canonical source and derives this list on load.
    messages: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list, repr=False)
    mcp: McpManager | None = None
    native: NativeToolRuntime | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # One upstream prompt per session, while other sessions remain concurrent.
    turn_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    state_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    steer_event: threading.Event = field(default_factory=threading.Event, repr=False)
    steer_queue: deque[str] = field(default_factory=deque, repr=False)
    # Parallel message-id ledger preserves existing string queue semantics while
    # making client retries idempotent. These records are ephemeral (not Crew's
    # durable queue); the client retains ownership of undelivered messages.
    steer_pending_ids: deque[str] = field(default_factory=deque, repr=False)
    steer_receipts: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)
    # Steer message-ids consumed (marked "injected") during the CURRENT turn but
    # not yet confirmed by a clean turn end. KiroCrew/kiro-cli only report a
    # steer consumed when its turn ends cleanly: a cancelled/failed turn leaves
    # it for the caller to requeue. BYO consumes at a model boundary mid-turn,
    # so it records the ids here and, if that turn ends cancelled, rolls them
    # back to "cleared" (emitting steering_cleared) instead of lying "injected".
    turn_injected_steer_ids: list[str] = field(default_factory=list, repr=False)
    steer_accepting: bool = True
    active_turn: bool = False
    # Ephemeral registry lifecycle flags. Set under AcpServer._sessions_lock.
    # A previously fetched Session must not start a turn after replacement.
    replacing: bool = field(default=False, repr=False)
    retired: bool = field(default=False, repr=False)
    # Stable ephemeral correlation id for the current logical turn. It is
    # derived from the canonical session-event sequence and links routing,
    # provider/tool trace events and compact TurnTelemetry without depending on
    # when the Durable Run is materialized.
    active_turn_id: str = ""
    # Ephemeral control-plane diagnostics for the current turn. These values are
    # never persisted and let Runtime Inspector distinguish a Crew/client cancel
    # from an upstream transport failure.
    turn_started_at: float = 0.0
    cancel_requested_at: float = 0.0
    cancel_source: str = ""
    loaded_tool_names: list[str] = field(default_factory=list)
    # Per-process only: last observed Crew tool capability snapshot. It is not
    # persisted, not an authorization grant and contains no broker credentials.
    host_capability_fingerprint: str = field(default="", repr=False)
    # Runtime projection of event-sourced sticky routing state
    # (pack/type -> provider, model). routing/pin events are durable; this cache
    # is rebuilt on load and revalidated against current eligibility each turn.
    route_pins: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)
    # Ephemeral unified Agent Runtime state for the current process. Durable
    # child-task continuation is intentionally a later capability.
    agent_registry: AgentRegistry = field(default_factory=AgentRegistry, repr=False)
    team_registry: AgentTeamRegistry = field(default_factory=AgentTeamRegistry, repr=False)
    active_agent_plan_id: str = field(default="", repr=False)
    # Durable Run is persisted separately from the session transcript. These are
    # ephemeral pointers to the current Run -> Step -> Attempt.
    active_run_id: str = field(default="", repr=False)
    active_run_step_id: str = field(default="", repr=False)
    active_run_attempt_id: str = field(default="", repr=False)
    active_run_lease_token: str = field(default="", repr=False)
    active_run_lease_lost: bool = field(default=False, repr=False)
    active_run_lease_stop: Any = field(default=None, repr=False)
    active_run_lease_thread: Any = field(default=None, repr=False)
    # Optimistic storage revision: a stale ACP process may never overwrite the
    # canonical session event log with an old in-memory snapshot.
    storage_revision: int = field(default=0, repr=False)
    storage_loaded: bool = field(default=False, repr=False)


@dataclass
class PendingClientRequest:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
