"""Backend subprocess lifecycle for pooled MCP servers.

A :class:`Backend` is one real MCP subprocess (e.g. a ``slack-mcp`` launcher) the gateway spawned on behalf of one or more stubs. It owns
stdin/stdout pipes, tracks whether the backend's ``initialize`` response
advertised the ``kirocrew.caller-identity`` capability (pooled operation),
and exposes a graceful shutdown path that escalates to ``SIGKILL`` after a
deadline.

Milestone 1 scope: spawn, handshake via ``initialize``, graceful shutdown.
JSON-RPC fan-out routing and per-call identity injection live in
Milestone 3 when the full bridge ships.
"""

from __future__ import annotations

import asyncio
import base64  # noqa: F401
import binascii  # noqa: F401
import contextlib
import importlib as _importlib
import json  # noqa: F401
import logging
import os  # noqa: F401
import sys as _sys
import time
import uuid  # noqa: F401
from dataclasses import dataclass, field, replace  # noqa: F401
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Mapping, Optional  # noqa: F401

from kiro_crew import _facade_mirror, platform_compat
from kiro_crew.constants import (  # noqa: F401
    KIROCREW_SPAWNED_ENV,
    KIROCREW_SPAWNED_VALUE,
    SUBAGENT_TIMEOUT_SECS,
)
from kiro_crew.executors import image_executor, maintenance_executor  # noqa: F401
from kiro_crew.install_liveness import (
    POOLED_BACKEND_ENV,
    POOLED_BACKEND_VALUE,
    POOLED_RESPAWN_COMMAND_ENV,
)
from kiro_crew.json_line import (  # noqa: F401
    ID_PROBE_BYTES,
    parse_json_object_line,
    recover_line_id,
    recover_top_level_id,
)
from kiro_crew.mcp_caller import (  # noqa: F401
    CALLER_CAPABILITY_KEY,
    CALLER_META_KEY,
    INFLIGHT_CAPABILITY_KEY,
    INFLIGHT_NOTIFICATION,
    TENANT_META_KEY,
    CallerContext,
    build_caller_meta,
    build_tenant_meta,
)
from kiro_crew.mcp_gateway import backend_handshake as _backend_handshake
from kiro_crew.mcp_gateway import backend_leases as _backend_leases
from kiro_crew.mcp_gateway import backend_liveness as _backend_liveness
from kiro_crew.mcp_gateway import backend_mcp_apps as _backend_mcp_apps
from kiro_crew.mcp_gateway import backend_routing as _backend_routing
from kiro_crew.mcp_gateway import backend_stubs as _backend_stubs
from kiro_crew.mcp_gateway import hazards, lifecycle_log  # noqa: F401
from kiro_crew.mcp_gateway.apps import (  # noqa: F401
    WithheldTools,
    append_marker,
    extract_declared_ui_uris,
    extract_ui_resource_uri,
    strip_model_hidden_tools,
    write_spool,
)
from kiro_crew.mcp_gateway.backend_handshake import (
    _DEFAULT_INITIALIZE_TIMEOUT_SECS,
    _effective_initialize_timeout_secs,
)
from kiro_crew.mcp_gateway.backend_tmp import (
    allocate_backend_tmp,
    record_owner,
    sweep_backend_tmp,
    tmp_env,
)
from kiro_crew.mcp_gateway.backend_wire import _PendingRequest
from kiro_crew.mcp_gateway.image_budget import (  # noqa: F401
    line_may_carry_image_block,
    parse_image_bearing_frame,
    rewrite_image_frame,
)
from kiro_crew.mcp_gateway.pool import (  # noqa: F401
    READ_BUFFER_LIMIT_BYTES,
    RESPONSE_SPILL_THRESHOLD_BYTES,
)
from kiro_crew.mcp_gateway.spill import maybe_spill_response  # noqa: F401
from kiro_crew.mcp_gateway.tool_surface import ToolSurface, project_tool_surface  # noqa: F401
from kiro_crew.process_identity import failure_name, with_kill_failure  # noqa: F401
from kiro_crew.sandbox import (
    CANONICAL_TEMP_KEYS,
    classify_declared_temp_env,
    declared_temp_refusal_reasons,
    format_declared_temp_refusals,
)
from kiro_crew.security import redact
from kiro_crew.sel import SecurityEventLog  # noqa: F401

if False:  # typing-only import guard
    from kiro_crew.mcp_gateway.pool import PoolKey

logger = logging.getLogger(__name__)


@dataclass
class Backend:
    """Running MCP backend subprocess.

    Attributes are populated by :func:`spawn_backend`; consumers should
    treat the dataclass as read-only except for ``last_used_at`` (updated
    by the routing layer on each forwarded call) and ``stdin`` / ``stdout``
    (consumed by the bridge pumps added in Milestone 3).

    Pooling eligibility: the first stub's
    ``initialize`` result is cached and replayed to every later stub on the
    same backend (see ``forward_from_stub`` point 1). This is only correct
    for servers whose ``initialize`` is **session-independent** — i.e. it
    returns the same capabilities regardless of which session connects first.
    The MCP spec does not require this; a server that negotiates per-session
    capabilities from ``clientInfo`` would silently hand session B session
    A's capability set when pooled. All MCP servers pooled today are
    session-independent. Verify this holds before adding a new server to the
    pool, or exclude it from pooling.
    """

    pool_key: "PoolKey"
    process: asyncio.subprocess.Process
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    created_at: float
    last_used_at: float
    supports_caller_identity: bool = False
    # The backend advertised ``mcp_shared.INFLIGHT_CAPABILITY_KEY``: it sends
    # ``notifications/kirocrew/inflight`` for its parked calls, so the wedge
    # detector holds it to PROGRESS_WEDGE_CEILING_SECS instead of the 3h ceiling.
    reports_inflight: bool = False
    # True only when the spawn that produced this process was Kiro Crew's own
    # packaged control plane -- the resolved command and args matched what the
    # managed spec emits, not merely a reserved server NAME. It gates the one
    # thing a pooled backend is ever handed beyond the caller's identity: the
    # session's bearer token. A server that merely calls itself
    # ``kirocrew-core`` stays False.
    control_plane: bool = False
    # Why ``control_plane`` is False for a backend spawned under a RESERVED name
    # (empty for a third-party name, and for an accepted control plane). Carried
    # to that backend on every forwarded frame as the caller's ``identity_denial``
    # so its ``identity_unattested`` refusal can say what the daemon saw; the
    # daemon's own log line is the only other place the reason exists.
    control_plane_denial: str = ""
    _shutdown_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # --- Sharing boundary state (Milestone 2) -------------------------------
    # Each attached stub appears in ``_stub_inboxes`` keyed by stub_uuid; the
    # inbox is a queue of backend->stub payloads (already-serialised bytes)
    # drained by the connection handler's writer task. ``refcount`` mirrors
    # ``len(_stub_inboxes)`` as a fast read-only integer so the idle-sweep
    # does not have to acquire the inbox lock on every pass.
    _stub_inboxes: dict[str, "asyncio.Queue[bytes]"] = field(default_factory=dict)
    # The agent each attached stub declared on its Register frame, keyed by the
    # same stub_uuid. ONE reader: ``_fetch_and_deliver_ui`` stamps it into the
    # spool record so an app callback can be governed by the agent that actually
    # produced the render (``app_call._agent_for_call``). It is per stub and not
    # a key field because the agent is not a pool dimension (see the ``pool``
    # module docstring), so a shared backend has several and a key field could
    # hold none of them. Kept in step with ``_stub_inboxes`` by
    # ``attach_stub``/``detach_stub`` under the same lock, which bounds it.
    _stub_agents: dict[str, str] = field(default_factory=dict)
    _inbox_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    refcount: int = 0
    # Non-empty on a backend bound to a single connection, holding that
    # connection's ``stub_uuid``. It makes ``storage_digest`` unique per
    # connection: PoolKey is identical across connections to the same server, so
    # without this discriminator two private backends would share a digest and
    # an app callback could resolve onto the wrong session's process.
    exclusive_token: str = ""
    # ``pinned`` marks a backend that the warm-pool prewarmer created ahead of
    # any stub. Such a backend sits at ``refcount == 0`` indefinitely (no stub
    # stays attached to it between chats), so the ordinary idle/LRU rules would
    # reclaim the very backend prewarming exists to keep ready. A pinned backend
    # is therefore exempt from idle eviction and from LRU victim selection; the
    # heartbeat sweeper still recycles it if it dies, and the credential-refresh
    # drain can force it down explicitly. Pinning is an out-of-band readiness
    # flag — it does NOT alter the backend's PoolKey, so per-session isolation
    # for every key dimension is fully preserved.
    pinned: bool = False
    # ``forward_id`` is a monotonic counter for rewriting request ids so two
    # stubs using the same integer id do not collide on the backend. Each
    # forwarded request is remembered in ``_pending_requests`` so the stdout
    # pump can route the response back to the originating stub with the
    # original id restored.
    _forward_id_seq: int = 0
    _pending_requests: dict[str, "_PendingRequest"] = field(default_factory=dict)
    # Resource-subscription routing table: ``uri -> {stub_uuid}``, the set of
    # stubs whose ``resources/subscribe`` the server ACCEPTED. Grants are
    # recorded on the server's response, never at forward time, so an update
    # racing an unconfirmed subscribe fails closed (dropped) instead of being
    # delivered on a subscription the server may yet refuse — including a
    # refusal made on per-caller authorization grounds. The notification
    # carries only the URI — never resource content — so delivering it to a
    # stub whose subscription for that URI was accepted discloses nothing
    # across tenants.
    _resource_subscriptions: dict[str, set[str]] = field(default_factory=dict)
    # Lease bookkeeping for the coalesced (identity-less) path. A URI in
    # ``_lease_awaiting_grant`` has its first subscribe forwarded and not yet
    # answered; stubs joining during that window are parked in
    # ``_lease_pending_riders`` as ``(stub_uuid, original_id)`` and answered
    # with the server's actual verdict, so no stub is ever told "subscribed"
    # on the strength of a lease that then fails to materialize. A URI in
    # ``_lease_awaiting_release`` has its final unsubscribe forwarded and not
    # yet answered; routing keeps the leaving stub until the server confirms,
    # because a failed unsubscribe means the server retained the subscription
    # and dropping routing early would silently discard live updates.
    _lease_awaiting_grant: set[str] = field(default_factory=set)
    _lease_awaiting_release: set[str] = field(default_factory=set)
    _lease_pending_riders: dict[str, list[tuple[str, Any]]] = field(default_factory=dict)
    # Final unsubscribes arriving while a URI's release is already in flight
    # park here as ``(stub_uuid, original_id)`` — the release need not belong
    # to the parking stub (its owner may have detached), so refusing would
    # dead-end a legitimate leave. Exactly one release stays in flight per
    # URI; every waiter settles on that one verdict: success drops the
    # waiter from routing and answers it, refusal keeps it routed and hands
    # it the server's error so it knows the unsubscribe did not take effect.
    _lease_release_waiters: dict[str, list[tuple[str, Any]]] = field(default_factory=dict)
    # Subscribes arriving while a URI's release is in flight park here. The
    # ordered stream only orders the WRITES; the server may ANSWER out of
    # order, so forwarding a replacement subscribe beside the release could
    # land a grant the release then destroys upstream while local routing
    # keeps the subscriber — silent update loss. A confirmed release drains
    # these into one fresh forwarded subscribe (first parker as forwarder,
    # the rest as riders); a refusal means the server retained the lease, so
    # they join the still-live entry locally.
    _lease_replacement_subscribes: dict[str, list[tuple[str, Any]]] = field(
        default_factory=dict)
    # Identity-capable servers only: the caller whose subscribe the server
    # GRANTED, keyed ``(uri, stub_uuid)`` and recorded at response time — the
    # only moment the grant principal is known for certain. A release for a
    # departing stub is sent as THIS caller (never the connection's caller at
    # teardown, and never a last-writer map: a claim rekey after the grant
    # makes both identify the wrong principal and revoke a co-tenant's live
    # subscription). Dropped when the grant is released or the entry pruned.
    _grant_callers: dict[tuple[str, str], "CallerContext"] = field(
        default_factory=dict)
    # URIs whose upstream subscription is known RETAINED with no subscriber
    # left to route to — a gateway-originated release was refused. Updates for
    # these are dropped WITHOUT recording a hazard: the frames are a
    # consequence of the broker's own lease handling, and recording them
    # would withdraw the server's pooling recommendation for behaviour that
    # is correct. Cleared when a later release succeeds or a new grant makes
    # the URI attributable again.
    _orphaned_leases: set[str] = field(default_factory=set)
    # Replay-target URIs preserved at death. An in-flight replay lives only
    # in ``_pending_requests`` (routing commits on the server's grant), so a
    # replacement that dies before its replay responses arrive would erase
    # those URIs in the backend-gone cleanup — and the NEXT respawn's
    # ``resource_subscription_uris`` capture would find them nowhere,
    # permanently darkening the subscription. Written once by
    # ``_broadcast_backend_gone`` (terminal state, size bounded by the
    # pending table it snapshots), read by the capture.
    _gone_replay_uris: dict[str, set[str]] = field(default_factory=dict)
    # Per-stub rekey generation, bumped by ``evict_stub_subscriptions``. A
    # multi-URI replay awaits between writes; a claim landing mid-loop
    # evicts the pendings written SO FAR, but the loop would keep writing
    # the remaining URIs under the old owner — this counter lets the replay
    # notice the rekey between writes and stop (fail closed: the new owner
    # subscribes on its own).
    _rekey_generation: dict[str, int] = field(default_factory=dict)
    # Initialize-cache state — first stub triggers an upstream handshake,
    # later stubs receive a synthesized response built from the cached result.
    _init_result: Optional[dict[str, Any]] = None
    _init_state: str = "unsent"  # "unsent" | "in_flight" | "ready"
    _init_pending: list[tuple[str, Any]] = field(default_factory=list)
    _init_first_stub: Optional[str] = None
    _init_first_id: Any = None
    # The ``initialize`` frame this backend was handshaken with, as forwarded
    # upstream minus its id. Kept so a backend whose server lost its MCP session
    # behind the pipe can be handshaken again (``_retry_after_rehandshake``):
    # kiro-cli sends ``initialize`` once per session and the cache above answers
    # every later stub, so no stub will ever send another one.
    _upstream_init_frame: Optional[dict[str, Any]] = None
    # Set once the upstream initialize resolves (ready OR failed). The
    # transparent-respawn path (gatewayd) awaits this after re-priming a
    # freshly spawned backend so stub traffic only resumes when the new
    # backend is handshake-complete.
    _init_done_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Bounds the FIRST upstream handshake. A backend that is alive but never
    # answers ``initialize`` leaves ``_init_state`` at "in_flight" forever:
    # every queued stub waits on a reply that never comes, and because the
    # process has not died no death-driven recovery (breaker, zombie sweep)
    # observes it. The timer turns that silence into the terminal "failed"
    # state and reaps the process group. Respawn priming carries its own
    # bounded wait, so only the lazy first handshake arms this.
    _init_deadline_task: Optional[asyncio.Task[None]] = None
    # Bound on the first ``initialize`` window, set at construction from the
    # daemon's configured value (``mcp_gateway.initialize_timeout_secs``). A
    # field rather than a module constant so the configured value reaches every
    # backend spawned by this daemon; the spawn-gate watcher reads the same
    # field, which is what keeps its permit window and this deadline aligned.
    initialize_timeout_secs: float = _DEFAULT_INITIALIZE_TIMEOUT_SECS
    _dead_reason: Optional[str] = None
    # Idempotency guard for _broadcast_backend_gone (see there): the terminal
    # "backend gone" broadcast is reachable near-simultaneously from several
    # paths, and re-running it double-delivers error replies to every stub.
    _gone_broadcast: bool = False
    _stdout_task: Optional[asyncio.Task[None]] = None
    # The stderr-drain task is tracked so shutdown()
    # can cancel it. Without a stored ref it (a) is only weakly held by the
    # loop and (b) outlives shutdown if the process survives SIGKILL, leaking
    # its stderr pipe fd across LRU-eviction churn.
    _stderr_task: Optional[asyncio.Task[None]] = None
    # The process's start id, read once right after spawn, while nothing can
    # have reaped it yet. The out-of-band backend record publishes this, not a
    # fresh read at heartbeat time: by then the backend may have exited and
    # its pid been handed to an unrelated process, whose start id a fresh read
    # would publish as this backend's. ``None`` when it could not be read; the
    # record then carries a bare pid, which no reap signals.
    start_id: Optional[str] = None
    # Quarantine: no new stubs may attach; kill when refcount drains to 0.
    quarantined: bool = False
    # Ping-gated wedge detection: updated each time the heartbeat ping response
    # is swallowed in _route_backend_line. Initialized to creation time so a
    # cold-start backend isn't immediately considered stale.
    _last_ping_response_mono: float = 0.0
    # Track request ids already warned as slow-but-responsive to avoid log spam.
    _warned_slow_ids: set = field(default_factory=set)
    # Best-effort latency-metric emits, fired off the stdout-pump hot path so a
    # slow/NFS metrics volume cannot add head-of-line latency to frame routing.
    # Tracked (with a discard done-callback) so a task is not GC'd before it
    # runs; still-pending emits are simply dropped on shutdown (best-effort).
    _metric_tasks: set["asyncio.Task[None]"] = field(default_factory=set)

    # Background tasks driving an MCP Apps ui:// fetch + delayed delivery (one
    # per intercepted tools/call). Tracked (discard-on-done) so a task is not
    # GC'd mid-flight; still-pending ones are dropped on shutdown (best-effort).
    _apps_tasks: set["asyncio.Task[None]"] = field(default_factory=set)
    # Tool-name -> declared ui:// resource uri, harvested from every
    # tools/list response (SEP-1865's primary association form lives on the
    # tool DECLARATION; some servers omit it from call results). Consulted as
    # a fallback by _maybe_intercept_ui_result. Backend-scoped: entries are
    # only ever produced by THIS backend's own tools/list responses.
    #
    # GLOBAL (not per-session) by design: the tool→ui:// association is a
    # STATIC property of the server's tool definition — identical for every
    # caller. kiro-cli harvests it on the post-initialize tools/list (which
    # carries no per-turn caller) while the eventual tools/call carries a later
    # session identity, so harvest-session and call-session legitimately differ
    # (see test_declared_only_server_still_intercepted). Keying this map per
    # session would drop the association and break rendering.
    _apps_declared_uris: dict[str, str] = field(default_factory=dict)
    # The tool set this backend TOLD each stub about, projected by
    # ``tool_surface`` from that stub's most recent model-facing tools/list
    # response. A missing entry = nothing was ever served to that stub, which is
    # NOT an empty tool set: it is the absence of any claim a replacement could
    # contradict.
    #
    # Keyed PER STUB, not per backend. A pool key carries no caller identity, so
    # one backend legitimately serves several sessions; a single scalar would let
    # the last listing served to ANY of them stand in for what a particular
    # session was told. For a server that answers every caller alike the two are
    # the same value, but the comparison must be about the session being
    # recovered, not about whichever tenant listed most recently.
    #
    # Bounded by the attached-stub set: ``detach_stub`` prunes an entry with the
    # rest of that stub's per-stub state, which is also why the respawn path
    # reads the anchor BEFORE it detaches.
    #
    # Read only by the respawn adoption path. Deliberately not an
    # authorization input — see the module docstring of
    # :mod:`kiro_crew.mcp_gateway.tool_surface` for why a snapshot must never
    # become one.
    _served_tool_surfaces: dict[str, ToolSurface] = field(default_factory=dict)

    # Log join keys (``lifecycle_log``): one generation per process, one
    # attempt per launch or re-sent ``initialize``. Never read by any decision.
    backend_generation_id: str = field(default_factory=lifecycle_log.new_lifecycle_id)
    attempt_id: str = field(default_factory=lifecycle_log.new_lifecycle_id)

    def __post_init__(self) -> None:
        # Serialize concurrent writes to the SHARED backend stdin. Every
        # co-pooled stub's forward_from_stub, initialize handling, prime, and
        # the heartbeat sweeper write to this SAME writer; two coroutines
        # inside write()+drain() on a paused transport trip CPython's
        # _drain_helper assert. _write_json_line acquires this per-backend lock
        # (mirrors gatewayd's per-connection _mc_write_lock on the outbound side).
        setattr(self.stdin, "_mc_write_lock", asyncio.Lock())

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid

    def lifecycle_fields(self, event: str, reason: Optional[str] = None) -> str:
        """This backend's log join fields for ``event`` (see ``lifecycle_log``)."""
        return lifecycle_log.lifecycle_fields(
            event,
            reason,
            generation=self.backend_generation_id,
            attempt=self.attempt_id,
        )

    @property
    def is_alive(self) -> bool:
        return self.process.returncode is None and self._dead_reason is None

    @property
    def dead_reason(self) -> Optional[str]:
        return self._dead_reason

    @property
    def outstanding_work(self) -> int:
        """Count of client responses this backend still OWES a stub.

        Three sources, because a response can be owed at three different stages
        and each one alone is an incomplete signal:

        1. ``_pending_requests`` — forwarded requests awaiting a backend reply.
           Every entry is keyed by a gateway-minted ``_next_forward_id()`` frame
           id. The gateway's own heartbeat ping is written straight to the
           backend's stdin under the reserved :data:`HEARTBEAT_PING_ID` and is
           deliberately NOT registered here, so an idle backend cannot look busy.
        2. Unfinished ``_apps_tasks`` — MCP Apps interception
           (:meth:`_fetch_and_deliver_ui`) consumes the pending entry and then
           does the out-of-band ``resources/read`` + spool write + delivery in a
           background task.
        3. Queued ``_stub_inboxes`` frames — the stdout pump pops the pending
           entry and ENQUEUES serialised bytes; the connection handler's writer
           task drains that queue onto the stub socket. Between those two steps
           the reply exists but has not reached the stub.

        Counting only (1) would let a shutdown cancel the connection while a
        completed reply sat in a queue or a delivery task, silently losing it.

        Used as the shutdown drain predicate (see
        ``gatewayd._has_outstanding_work``) — "is a response still owed?" —
        which is a different question from ``refcount`` ("is a stub attached?").
        A pooled stub stays attached for the life of its session, so refcount
        never falls to zero on its own and is useless as a drain signal.
        """
        queued = sum(inbox.qsize() for inbox in list(self._stub_inboxes.values()))
        unfinished_apps = sum(1 for task in self._apps_tasks if not task.done())
        return len(self._pending_requests) + unfinished_apps + queued

    @property
    def storage_digest(self) -> str:
        """The pool's key for this backend, and the identity an app callback
        resolves against.

        Equal to the PoolKey digest for a shared backend -- two connections that
        may share a process resolve to the same entry, which is the point. A
        connection-private backend appends its ``exclusive_token`` so it is
        addressable without being reachable from any other connection.
        """
        base = self.pool_key.stable_hash()
        return f"{base}:{self.exclusive_token}" if self.exclusive_token else base

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def touch(self, now: Optional[float] = None) -> None:
        """Mark the backend as freshly used. Called by the routing layer
        on every forwarded request so the idle-sweep (Milestone 2) can tell
        real traffic from accumulated stragglers."""
        self.last_used_at = now if now is not None else time.monotonic()

    def agent_for_stub(self, stub_uuid: str) -> str:
        """The agent ``stub_uuid`` attached under, or ``""``.

        The governance identity of any app render this stub's calls produce (see
        :meth:`_fetch_and_deliver_ui`). ``""`` is a refusing answer downstream,
        never a default agent. A transparent respawn also reads it to carry the
        label onto the replacement backend, which holds no Register frame of its
        own.

        Lock-free on purpose — ``dict`` reads are atomic under the one event
        loop every mutation also runs on, and the interception path must not
        queue behind an attach.
        """
        return self._stub_agents.get(stub_uuid, "")

    # The stub side of a shared ``Backend``: ``kiro_crew.mcp_gateway.backend_stubs``.
    attach_stub = _backend_stubs.attach_stub
    detach_stub = _backend_stubs.detach_stub
    _next_forward_id = _backend_stubs._next_forward_id
    forward_from_stub = _backend_stubs.forward_from_stub
    served_tool_surface = _backend_stubs.served_tool_surface
    carry_served_tool_surface = _backend_stubs.carry_served_tool_surface
    probe_tool_surface = _backend_stubs.probe_tool_surface
    _enqueue_to_stub = _backend_stubs._enqueue_to_stub
    _deliver_to_stub = _backend_stubs._deliver_to_stub
    _broadcast = _backend_stubs._broadcast
    _reply_locally = _backend_stubs._reply_locally
    cancel_in_flight_for_stub = _backend_stubs.cancel_in_flight_for_stub
    # The ``resources/subscribe`` lease broker of a shared ``Backend``: ``kiro_crew.mcp_gateway.backend_leases``.
    evict_stub_subscriptions = _backend_leases.evict_stub_subscriptions
    _handle_resource_subscription = _backend_leases._handle_resource_subscription
    _on_resource_subscription_response = _backend_leases._on_resource_subscription_response
    resource_subscription_uris = _backend_leases.resource_subscription_uris
    replay_resource_subscriptions = _backend_leases.replay_resource_subscriptions
    _stub_subscription_count = _backend_leases._stub_subscription_count
    _drain_replacement_subscribes = _backend_leases._drain_replacement_subscribes
    _release_upstream_subscriptions = _backend_leases._release_upstream_subscriptions
    _resource_update_targets = _backend_leases._resource_update_targets
    # The MCP ``initialize`` handshake of a ``Backend``: ``kiro_crew.mcp_gateway.backend_handshake``.
    _handle_initialize = _backend_handshake._handle_initialize
    _arm_init_deadline = _backend_handshake._arm_init_deadline
    _cancel_init_deadline = _backend_handshake._cancel_init_deadline
    _init_deadline = _backend_handshake._init_deadline
    _deliver_cached_initialize = _backend_handshake._deliver_cached_initialize
    prime_initialize = _backend_handshake.prime_initialize
    _retry_after_rehandshake = _backend_handshake._retry_after_rehandshake
    _fail_init = _backend_handshake._fail_init
    _on_upstream_initialize = _backend_handshake._on_upstream_initialize
    # Liveness and teardown of a ``Backend``: ``kiro_crew.mcp_gateway.backend_liveness``.
    _broadcast_backend_gone = _backend_liveness._broadcast_backend_gone
    _heartbeat_once = _backend_liveness._heartbeat_once
    _cancel_background_tasks = _backend_liveness._cancel_background_tasks
    recycle_if_idle = _backend_liveness.recycle_if_idle
    shutdown = _backend_liveness.shutdown
    # The stdout pump of a ``Backend``: ``kiro_crew.mcp_gateway.backend_routing``.
    run_stdout_pump = _backend_routing.run_stdout_pump
    _isolate_stdout_line = _backend_routing._isolate_stdout_line
    _handle_stdout_line = _backend_routing._handle_stdout_line
    _route_backend_line = _backend_routing._route_backend_line
    _fail_unparseable_response = _backend_routing._fail_unparseable_response
    _fail_claimed_request = _backend_routing._fail_claimed_request
    _settle = _backend_routing._settle
    _route_backend_message = _backend_routing._route_backend_message
    _record_hazard = _backend_routing._record_hazard
    _apply_inflight_report = _backend_routing._apply_inflight_report
    _notification_owner = _backend_routing._notification_owner
    _spawn_metric_task = _backend_routing._spawn_metric_task
    _fail_oversize_request = _backend_routing._fail_oversize_request
    # MCP Apps on a shared ``Backend``: ``kiro_crew.mcp_gateway.backend_mcp_apps``.
    _maybe_intercept_ui_result = _backend_mcp_apps._maybe_intercept_ui_result
    _audit_visibility_withhold = _backend_mcp_apps._audit_visibility_withhold
    _fetch_and_deliver_ui = _backend_mcp_apps._fetch_and_deliver_ui
    _read_ui_resource = _backend_mcp_apps._read_ui_resource
    _parse_ui_contents = _backend_mcp_apps._parse_ui_contents


async def spawn_backend(
    pool_key: "PoolKey",
    command: str,
    args: list[str],
    env: Mapping[str, str],
    work_dir: str,
    declared_temp_keys: tuple[str, ...] = (),
    secret_env_keys: tuple[str, ...] = (),
    initialize_timeout_secs: float = _DEFAULT_INITIALIZE_TIMEOUT_SECS,
) -> Backend:
    """Spawn a real MCP subprocess and wrap it in a :class:`Backend`.

    ``initialize_timeout_secs`` bounds the backend's first ``initialize``
    window (``Backend.initialize_timeout_secs``); the daemon threads its
    configured value here so a constructor argument, not a module setter,
    carries it.

    ``declared_temp_keys`` are the temp-key names (``TMPDIR``/``TMP``/``TEMP``,
    any casing) the operator's agent spec DECLARES for this server -- the
    caller knows the declared-env set and this function does not (``env``
    also carries the daemon's ambient values, which must not suppress
    containment; see the containment block below). ``secret_env_keys`` marks
    values resolved from ``secret://`` URIs so a refusal can name its key and
    cause without persisting the resolved value in the daemon log.

    ``env`` is the complete effective environment; callers MUST NOT rely on
    parent-process inheritance. The temp rule may replace its declared temp
    keys, and this function adds the positive spawn marker. The PoolKey still
    hashes the caller's original effective map, so a refused declaration may
    conservatively split two equivalent managed-temp backends but can never
    collapse specs that declared different environments into one pool.

    Security boundary (accepted risk, documented in
    ``docs/system-specs/modules/security.md`` under MCP Gateway): backends
    spawned here do NOT run inside a Linux mount namespace. The per-session
    sandbox applied in ``AcpClient._spawn()`` protects kiro-cli sessions,
    not gateway-spawned backends. Compensating controls:

    1. ``command`` is taken verbatim from ``KIROCREW_MCP_TARGET_<SERVER>`` env
       vars populated at KiroCrew startup by the rewriter from the user's
       own ``~/.kiro/agents/*.json``. Stubs cannot cause gatewayd to spawn
       an arbitrary binary — only pre-approved MCP servers.
    2. ``GatewayManager._scrub_sensitive_env()`` strips AWS / SSH / GPG /
       git credential env vars before gatewayd inherits them, so spawned
       backends do not inherit credential env (file-level access to
       ``~/.aws`` etc. is a known residual risk — backends that need AWS
       credentials read them from disk via ``ada`` / default credential
       chain, same as today's non-pooled topology).
    3. Backends run as the invoking user's UID, same as kiro-cli —
       the pool does not elevate privileges.

    Tightening this to a full mount namespace for pooled backends is tracked
    as Phase-2 hardening; broader rollout is gated on it.
    """
    # Minted before the process exists so the spawn line carries the same keys
    # as every later line about this backend.
    generation_id = lifecycle_log.new_lifecycle_id()
    attempt_id = lifecycle_log.new_lifecycle_id()
    logger.info(
        "spawning backend pool=%s command=%s args=%s %s",
        pool_key.human_readable(),
        command,
        redact(" ".join(args)),
        lifecycle_log.lifecycle_fields(
            "initialize_started", generation=generation_id, attempt=attempt_id
        ),
    )
    # Positive-identity marker for the orphan sweep. Safe re: the pooled-backend
    # PoolKey invariant — the marker is a compile-time constant, so it is
    # identical for every key and cannot split or collapse pooled-backend
    # identity (unlike a per-session value, which would be a correctness bug).
    spawn_env = dict(env)
    spawn_env[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE
    # Tells a first-party server it may exit to be replaced (its install was
    # pruned): this pool respawns it. Constant, so PoolKey-safe like the above.
    spawn_env[POOLED_BACKEND_ENV] = POOLED_BACKEND_VALUE
    # The command a respawn reuses (the daemon's target map is frozen), so the
    # server exits only when that relaunch can still work. Derived from the
    # key's own command, so PoolKey-safe like the marker.
    spawn_env[POOLED_RESPAWN_COMMAND_ENV] = command
    # Per-process temp containment. Safe re: the pooled-backend
    # PoolKey invariant for the same reason as the marker above -- the value
    # is derived from the key's own digest plus a token generated AFTER
    # pool-identity resolution and is never folded into the hash, so it can
    # neither split nor collapse pool identity. Allocation and declaration
    # classification run off-loop because both touch the filesystem.
    #
    # An operator-declared temp wins only when the shared sandbox rule clears
    # every declared key. One refusal drops the whole declaration: ``tempfile``
    # may consult a sibling key first, so keeping a surviving key would make
    # the result depend on spelling order. The managed temp takes over. If its
    # allocation fails after a refusal, no temp key survives to point the child
    # back at the refused path.
    #
    # Declaration is signalled by the caller, not inferred from ``spawn_env``.
    # The resolver folds the daemon's ambient temp into ``env`` on macOS and
    # Windows, and an env-membership test would mistake that for operator input.
    backend_tmp: Optional[Path] = None
    _declared_upper: set[str] = set()
    refused: dict[str, tuple[str, str]] = {}
    failure = ""
    if declared_temp_keys:
        accepted, refused, failure = await asyncio.to_thread(
            classify_declared_temp_env,
            spawn_env,
            declared_temp_keys,
        )
        _declared_upper = set(accepted)
        if refused:
            logger.warning(
                "MCP backend [%s]: ignoring spec-declared %s — %s; spawning with the "
                "managed temp instead",
                pool_key.server_name,
                format_declared_temp_refusals(
                    refused,
                    hidden_keys=secret_env_keys,
                    redactor=redact,
                ),
                "; ".join(
                    declared_temp_refusal_reasons(
                        refused,
                        failure,
                        redactor=redact,
                    )
                ),
            )
            spawn_env = {
                key: value
                for key, value in spawn_env.items()
                if key.upper() not in CANONICAL_TEMP_KEYS
            }
    if not _declared_upper:
        try:
            backend_tmp = await asyncio.to_thread(allocate_backend_tmp, pool_key.stable_hash())
            spawn_env.update(tmp_env(backend_tmp))
        except OSError:
            has_inherited_temp = any(key.upper() in CANONICAL_TEMP_KEYS for key in spawn_env)
            fallback = "inherited temp" if has_inherited_temp else "platform default"
            logger.warning(
                "backend-tmp: could not allocate a contained temp dir; spawning with %s",
                fallback,
                exc_info=True,
            )
    else:
        # Keep only the declared values and re-emit their canonical spellings.
        # Ambient siblings cannot outrank the declaration, and lowercase spec
        # keys govern on POSIX as well as on case-insensitive Windows env maps.
        declared_values = {
            key.upper(): value for key, value in spawn_env.items() if key.upper() in _declared_upper
        }
        spawn_env = {
            key: value for key, value in spawn_env.items() if key.upper() not in CANONICAL_TEMP_KEYS
        }
        spawn_env.update(declared_values)
    try:
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            env=spawn_env,
            start_new_session=True,
            limit=READ_BUFFER_LIMIT_BYTES,
        )
    except BaseException:
        # The spawn itself failed: reclaim the just-allocated dir here and
        # now. Unowned directories are deliberately never deleted by the
        # sweeps (an owner record can fail on a live backend), so this is
        # the ONLY reclamation point for a dir whose process never existed.
        if backend_tmp is not None:
            await asyncio.to_thread(sweep_backend_tmp, backend_tmp)
        raise
    # Read before the first await: the child watcher cannot have reaped the
    # process yet, so this is the backend's own identity (see Backend.start_id).
    start_id = platform_compat.get_process_start_id(process.pid)
    if process.stdin is None or process.stdout is None:
        # asyncio.create_subprocess_exec populates these whenever PIPE was
        # requested; the guard exists for type checkers. Kill the child on
        # this (practically-unreachable) path so it can't outlive the raise.
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise RuntimeError("subprocess pipes not attached")

    # Drain stderr in the background so a chatty backend can't fill the OS
    # pipe buffer and wedge itself. We log every line at DEBUG. The task ref
    # is stored on the Backend so shutdown() can cancel it
    # and release the stderr pipe fd; otherwise it is only weakly held and
    # leaks if the process outlives a SIGKILL.
    stderr_task: Optional[asyncio.Task[None]] = None
    if process.stderr is not None:
        stderr_task = asyncio.create_task(
            _pump_stderr(process.stderr, pool_key.human_readable()),
            name=f"mcp-gateway-backend-stderr-{process.pid}",
        )

    now = time.monotonic()
    backend = Backend(
        pool_key=pool_key,
        process=process,
        stdin=process.stdin,
        stdout=process.stdout,
        created_at=now,
        last_used_at=now,
        initialize_timeout_secs=_effective_initialize_timeout_secs(initialize_timeout_secs),
        backend_generation_id=generation_id,
        attempt_id=attempt_id,
    )
    backend._last_ping_response_mono = now  # cold-start: not insta-stale
    backend._stderr_task = stderr_task
    backend.start_id = start_id
    if backend_tmp is not None:
        # Liveness anchor for the daemon-boot sweep: a SIGKILL'd daemon can
        # leave this backend running (start_new_session), and the next boot
        # must not delete temp storage under a survivor. Off-loop, fail-open
        # (an unowned dir falls under the sweep's grace-window rule instead).
        await asyncio.to_thread(record_owner, backend_tmp, process.pid)
    return backend


async def _pump_stderr(reader: asyncio.StreamReader, label: str) -> None:
    """Consume a backend's stderr line by line at DEBUG level."""
    while True:
        try:
            line = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError):
            # An oversize (>limit) stderr line: readline() drops it from the
            # buffer and raises. Skip it and keep draining — returning here
            # would let the stderr pipe fill and wedge the backend (the exact
            # self-wedge this drain exists to prevent).
            continue
        except Exception:  # pragma: no cover — reader closed during shutdown
            return
        if not line:
            return
        # DEBUG intentionally — backend stderr is routinely verbose
        # (tracing/log crate output) and would otherwise flood INFO logs.
        # redact() so a secret printed to stderr (e.g. a token fragment in a
        # stack trace) does not land verbatim in the KiroCrew log.
        logger.debug(
            "backend[%s] stderr: %s",
            label,
            redact(line.decode("utf-8", errors="replace").rstrip()),
        )


# --------------------------------------------------------------------------- #
# Compatibility facade. ``Backend``'s behaviour lives in the ``backend_*`` owner
# modules listed below, one responsibility each: a member's body is a module-level
# function there whose first parameter is ``self``, and the class binds it by name, so
# every member stays an attribute of the one class and the backend's state stays on
# the instance. The module-level names those members read live in the owners too.
# Every name stays readable as ``kiro_crew.mcp_gateway.backend.<name>``: this module
# answers a name it does not bind from the owner that defines it, and a write or
# delete through this module reaches every owner binding the same object
# (``kiro_crew._facade_mirror``), so a patch of ``kiro_crew.mcp_gateway.backend.<name>``
# reaches every reader it reached when this was one module. Owners never import this
# module at run time and log under its logger. ``test/test_backend_bindings.py`` pins
# the member inventory, the binding and the reach.
# --------------------------------------------------------------------------- #
#: The backend's owner modules, lowest layer first: an owner imports only owners listed
#: before it, and the first holder of a name answers a read of it.
_BACKEND_OWNERS: tuple[str, ...] = (
    "kiro_crew.mcp_gateway.backend_wire",
    "kiro_crew.mcp_gateway.backend_leases",
    "kiro_crew.mcp_gateway.backend_mcp_apps",
    "kiro_crew.mcp_gateway.backend_handshake",
    "kiro_crew.mcp_gateway.backend_stubs",
    "kiro_crew.mcp_gateway.backend_liveness",
    "kiro_crew.mcp_gateway.backend_routing",
)
for _owner in _BACKEND_OWNERS:
    _importlib.import_module(_owner)


def _surface() -> frozenset[str]:
    """The names this module answers for: its own, and every name an owner binds that
    is not a ``Backend`` member, which the class binds itself.

    A name an owner adds is therefore part of this module too: one it defines joins the
    ``TYPE_CHECKING`` manifest below, one it imports is imported here as well, and
    the mirror guards (``test/test_facade_mirror_guards.py``, ``test/test_backend_bindings.py``)
    fail until it is."""
    members = frozenset(vars(Backend))
    owned = frozenset(
        name
        for dotted in _BACKEND_OWNERS
        for name in vars(_sys.modules[dotted])
        if name not in members
    )
    return frozenset(globals()) | owned


#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_MIRROR = _facade_mirror.install(
    _sys.modules[__name__], _BACKEND_OWNERS, excluded=(), surface=_surface()
)
_EXPORTS.update(_MIRROR.served)
# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(
    name
    for name in set(globals()) | set(_EXPORTS)
    if not name.startswith("_") and name != "TYPE_CHECKING"
)

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.mcp_gateway.backend_handshake import (  # noqa: F401
        _GATEWAY_INIT_ID,
        _LOST_SESSION_ERROR_CODE,
        _LOST_SESSION_ERROR_MESSAGE,
        _REHANDSHAKE_RETRY_METHODS,
        _REHANDSHAKE_STUB_SENTINEL,
        _WINDOWS_INITIALIZE_TIMEOUT_FLOOR_SECS,
        _inject_inflight_capability,
        _is_lost_session_error,
        _parse_initialize_response,
        _reports_inflight,
        _without_id,
        send_initialize,
    )
    from kiro_crew.mcp_gateway.backend_leases import (  # noqa: F401
        _ORPHANED_LEASES_MAX,
        _RELEASE_STUB_SENTINEL,
        _RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB,
        _RESOURCE_URI_MAX_LEN,
        _RESOURCES_SUBSCRIBE_METHOD,
        _RESOURCES_UNSUBSCRIBE_METHOD,
        _RESOURCES_UPDATED_NOTIFICATION,
        _is_success_response,
    )
    from kiro_crew.mcp_gateway.backend_liveness import (  # noqa: F401
        _RECYCLE_EXEMPT_METHODS,
        HARD_WEDGE_CEILING_SECS,
        HEARTBEAT_PING_ID,
        HEARTBEAT_TIMEOUT_SECS,
        PING_STALE_SECS,
        PROGRESS_WEDGE_CEILING_SECS,
        _is_heartbeat_id,
        has_recyclable_in_flight,
    )
    from kiro_crew.mcp_gateway.backend_mcp_apps import (  # noqa: F401
        _APPS_RESOURCE_READ_TIMEOUT_SECS,
        _APPS_STUB_SENTINEL,
        MCP_APPS_ENV_FALSE,
        MCP_APPS_ENV_FLAG,
        MCP_APPS_ENV_TRUE,
        MCP_APPS_EXTENSION_KEY,
        MCP_APPS_MIME_TYPE,
        _inject_client_extensions,
        _mcp_apps_enabled,
        mcp_apps_env_override,
    )
    from kiro_crew.mcp_gateway.backend_routing import (  # noqa: F401
        _GLOBAL_BROADCAST_NOTIFICATIONS,
        _METRICS_PATH,
        _emit_call_metric,
        _write_metric_line,
    )
    from kiro_crew.mcp_gateway.backend_stubs import (  # noqa: F401
        _STUB_INBOX_MAXSIZE,
        _TOOL_SURFACE_PROBE_TIMEOUT_SECS,
    )
    from kiro_crew.mcp_gateway.backend_wire import (  # noqa: F401
        _JSONRPC_SERVER_ERROR,
        _MCP_IDENT_LOG_MAX,
        _TOOL_ERROR_LOG_MAX,
        _TOOL_ERROR_PRESCAN_MAX,
        _WRITE_DRAIN_TIMEOUT_SECS,
        INTERNAL_STUB_PREFIXES,
        TOOL_SURFACE_STUB_PREFIX,
        BackendGone,
        _collapse_nonprintable,
        _inject_caller_meta,
        _inject_tenant_meta,
        _log_safe_identifier,
        _mcp_result_error_text,
        _strip_caller_meta,
        _tool_call_error_text,
        _write_json_line,
        _write_json_lines,
    )
