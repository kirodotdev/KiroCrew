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
import base64
import binascii
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from kiro_crew import platform_compat
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
from kiro_crew.executors import maintenance_executor
from kiro_crew.mcp_caller import (
    CALLER_CAPABILITY_KEY,
    CALLER_META_KEY,
    CallerContext,
    build_caller_meta,
)
from kiro_crew.mcp_gateway.apps import (
    append_marker,
    extract_declared_ui_uris,
    extract_ui_resource_uri,
    write_spool,
)
from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES, RESPONSE_SPILL_THRESHOLD_BYTES
from kiro_crew.mcp_gateway.spill import maybe_spill_response
from kiro_crew.security import redact
from kiro_crew.sel import SecurityEventLog

if False:  # typing-only import guard
    from kiro_crew.mcp_gateway.pool import PoolKey

logger = logging.getLogger(__name__)

# --- Per-request latency metrics ----------------------------------------
#
# When ``MCP_GATEWAY_CALL_METRICS_PATH`` is set, every completed request
# (any method — tools/call, tools/list, initialize, etc.) appends one
# JSON line with the exact gateway-e2e duration (``forward_from_stub``
# receive → backend stdout response routed). This is the only measurement
# in the system that isolates MCP gateway + backend wall time with zero
# LLM inference mixed in.
#
# Schema (one JSONL record per completed request):
#   {"ts": epoch_ms_int, "method": "tools/call", "dur_ms": 2.34,
#    "pool": "example-mcp::kirocrew::...", "pid": 12345, "ok": true}
#
# Ring-buffer-free — we trust log rotation on the consumer side.
_METRICS_PATH = os.environ.get("MCP_GATEWAY_CALL_METRICS_PATH")


def _write_metric_line(record: dict[str, Any]) -> None:
    """Synchronous jsonl append. Always run off the event loop via
    :func:`_emit_call_metric` — never call directly from a coroutine."""
    if _METRICS_PATH is None:  # narrows for mypy; also guarded in the caller
        return
    try:
        line = json.dumps(record, separators=(",", ":"))
        with open(_METRICS_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        # Disk full, permission denied, rotation race — drop silently.
        pass


async def _emit_call_metric(record: dict[str, Any]) -> None:
    """Best-effort append to the metrics jsonl. Silent on any IO error —
    latency instrumentation MUST NOT affect request routing correctness.

    The blocking ``open()``/``write()`` is offloaded via ``asyncio.to_thread``
    so a slow or NFS-backed metrics volume cannot stall the event loop (and
    thus all request routing). When disabled (no path configured) this is a
    cheap early return with no thread hop.
    """
    if not _METRICS_PATH:
        return
    await asyncio.to_thread(_write_metric_line, record)


# Default handshake deadline. Real MCP backends reply to ``initialize``
# within tens of milliseconds; 10s is generous slack for a cold-spawning
# backend on a loaded host.
_DEFAULT_INITIALIZE_TIMEOUT_SECS = 10.0

# Upper bound on a single ``_write_json_line`` drain (writing a forwarded
# request to a backend's stdin). A backend that has stopped reading its stdin
# must not hang the forwarding coroutine — and the shared heartbeat sweeper —
# forever. On timeout the write raises a pipe error so the caller recycles the
# wedged backend. Mirrors gatewayd's bounded reply drain.
_WRITE_DRAIN_TIMEOUT_SECS = 30.0

# JSON-RPC initialize id the gateway uses on behalf of the first stub.
# Real stubs' ``initialize`` requests are cached and replayed from the
# gateway-side ``init_cache``; this id only matters for the one-shot
# handshake in :func:`send_initialize`.
_GATEWAY_INIT_ID = "mcp-gateway-init-0"

# Reserved JSON-RPC id for gateway-internal liveness pings.
# A positive integer well inside the JSON-RPC safe-integer range (2**53-1) so
# backends that round-trip ids through a float Number type cannot truncate it,
# and trivially distinct from forwarded request ids which are
# ``"gw-<pid>-<n>"`` strings. Responses bearing this id are swallowed in
# :meth:`Backend._route_backend_line` and never delivered to a stub.
HEARTBEAT_PING_ID = 0x6D63_6862  # 1835100258 ("mchb")

# An in-flight request is considered *potentially* wedged if outstanding longer
# than this. However, shared backends (kirocrew-core) host legitimately long
# tools: ``wait`` (60-1800s) and ``spawn_sub_agents`` (blocking). A backend is
# only recycled when BOTH the oldest pending request exceeds this age AND the
# backend has not responded to a heartbeat ping within PING_STALE_SECS — a
# backend that answers pings is slow, not wedged.
#
# Exceeding this threshold alone must not force an immediate recycle: that
# would kill a high-refcount kirocrew-core backend that is still answering
# fresh pings.
HEARTBEAT_TIMEOUT_SECS = 300.0

# A backend's ping response is considered stale if no ping response has arrived
# within 2.5x the gatewayd heartbeat sweep interval (60s). This ensures at
# least 2 full sweep cycles pass before concluding the backend is unresponsive.
PING_STALE_SECS = 150.0

# Absolute ceiling: recycle regardless of ping freshness. Protects against a
# pathological case where the tool itself is stuck but the MCP server's read
# loop still services ping requests. Set to wait_max (1800s) + 5-min margin.
HARD_WEDGE_CEILING_SECS = 2100.0

# Upper bound on a single stub's pending-delivery inbox. Backend->stub frames
# are enqueued by the stdout pump without awaiting the stub's socket drain, so
# a stub that has stopped reading must not let a chatty backend grow gateway
# RSS without bound. Past this many undrained frames the slow stub is dropped
# (see ``Backend._enqueue_to_stub``) so co-pooled sessions are protected.
# Generous enough that normal bursts (large tools/list, rapid tool calls)
# never trip it.
_STUB_INBOX_MAXSIZE = 4096

# Server->client notifications that reflect backend-wide state identical for
# every co-pooled tenant, so they are safe to fan out when we cannot attribute
# them to a single owning stub. Everything else (progress, logging/message,
# cancelled, resource-update) is request- or subscription-scoped: broadcasting
# an unattributable one would leak one tenant's content to co-tenants — a
# disclosure the non-pooled baseline never had — so those are dropped instead.
_GLOBAL_BROADCAST_NOTIFICATIONS: frozenset[str] = frozenset({
    "notifications/tools/list_changed",
    "notifications/prompts/list_changed",
    "notifications/resources/list_changed",
})


def _is_heartbeat_id(msg_id: Any) -> bool:
    """True if ``msg_id`` is the reserved heartbeat ping id (int or its
    string form, since some backends stringify response ids)."""
    return msg_id == HEARTBEAT_PING_ID or str(msg_id) == str(HEARTBEAT_PING_ID)


class BackendGone(RuntimeError):
    """Raised when a caller tries to forward into a backend that is dead
    or has lost its stdin pipe. The connection handler catches this and
    emits a clean JSON-RPC error to the originating stub."""


@dataclass
class _PendingRequest:
    """Tracks an in-flight request so the stdout pump can restore the
    stub's original JSON-RPC id on the response. ``stub_uuid`` can also
    be the sentinel ``"__init__"`` for the gateway's upstream initialize
    request — that case is handled separately in :meth:`Backend._route_backend_line`.

    ``t_start_ms`` is the monotonic clock at forward time. The stdout
    pump uses it to compute an authoritative gateway-e2e duration for
    every completed request — the only point where we can claim "this is
    MCP gateway wall time" without LLM inference mixed in.
    """

    stub_uuid: str
    original_id: Any
    method: str
    t_start_ms: float = 0.0
    # The request's ``params._meta.progressToken`` if it set one, so a
    # server-emitted ``notifications/progress`` can be routed back to the
    # owning stub instead of broadcast across co-pooled tenants.
    progress_token: Any = None
    # MCP Apps: captured at forward time so the response-interception path can
    # populate the spool record without the original request params (which are
    # not otherwise retained). ``session_key`` is the authoritative caller id;
    # ``tool_name`` is ``params.name`` for a tools/call. Both empty for
    # non-tools/call requests and gateway-internal sentinels.
    session_key: str = ""
    tool_name: str = ""
    # The tools/call ``params.arguments`` object, captured so an intercepted
    # app render can forward the ORIGINATING inputs to the app (SEP-1865
    # ``ui/notifications/tool-input``). ``None`` for non-tools/call requests.
    tool_arguments: Optional[dict] = None
    # Set only on the gateway-originated ``resources/read`` pending (stub_uuid
    # == ``_APPS_STUB_SENTINEL``): the future the stdout pump resolves with the
    # backend's resources/read response so the parked fetch coroutine wakes.
    apps_future: Optional["asyncio.Future[dict[str, Any]]"] = None


def _strip_caller_meta(msg: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``msg`` with any stub-supplied
    ``params._meta.kirocrew.caller`` unconditionally removed.

    The gateway is the trust boundary: stubs are untrusted clients and must
    never be able to forge their caller identity by pre-populating
    ``_meta.kirocrew.caller`` in the request. This function is called on
    EVERY forwarded request regardless of method so a malicious stub cannot
    sneak a forged caller block through non-tools/call methods.
    """
    out = dict(msg)
    params = out.get("params")
    if not isinstance(params, dict):
        return out
    meta_raw = params.get("_meta")
    if not isinstance(meta_raw, dict) or CALLER_META_KEY not in meta_raw:
        return out
    # The caller block is a FLAT ``params._meta[CALLER_META_KEY]`` key
    # ("kirocrew.caller") — exactly the shape build_caller_meta writes and
    # CallerContext.from_meta reads. Strip that flat key. (An earlier nested
    # "_meta[kirocrew][caller]" strip was a no-op against the real wire format,
    # so a stub-forged block survived whenever no authoritative identity was
    # injected over it — e.g. a stub that registered without a session_key —
    # enabling cross-tenant identity forgery. Stripping on EVERY forwarded
    # request closes that regardless of whether injection happens.)
    params = dict(params)
    meta = dict(meta_raw)
    del meta[CALLER_META_KEY]
    if meta:
        params["_meta"] = meta
    else:
        del params["_meta"]
    out["params"] = params
    return out


# MCP Apps (SEP-1865): extension key + MIME profile the gateway advertises to
# backends when the feature flag is on. kiro-cli sends empty client
# capabilities, so UI-enabled tools would otherwise degrade to text-only.
# The gateway is the actual Apps host (it fetches/renders the ui:// resource
# out-of-band), so it — not kiro-cli — advertises the capability.
MCP_APPS_EXTENSION_KEY = "io.modelcontextprotocol/ui"
MCP_APPS_MIME_TYPE = "text/html;profile=mcp-app"
MCP_APPS_ENV_FLAG = "KIROCREW_MCP_APPS"
#: Tokens the env flag recognises. Module constants rather than literals inline
#: in the gate, because the dashboard's write path has to recognise the SAME set
#: to refuse a config write the env would override — two copies would drift.
MCP_APPS_ENV_TRUE = ("1", "true", "yes")
MCP_APPS_ENV_FALSE = ("0", "false", "no", "off")

# Sentinel ``stub_uuid`` for a gateway-originated ``resources/read`` issued to
# fetch a ui:// app resource. Its response is routed to the parked future in
# :meth:`Backend._read_ui_resource` instead of any stub — mirrors the
# ``"__init__"`` sentinel used for the gateway-driven initialize handshake.
_APPS_STUB_SENTINEL = "__apps__"

# Deadline for the out-of-band ``resources/read`` round-trip. On timeout the
# original tools/call response is delivered unmodified — the app render is
# best-effort and MUST NOT wedge or drop the tool result.
_APPS_RESOURCE_READ_TIMEOUT_SECS = 10.0


def mcp_apps_env_override() -> bool | None:
    """The env flag's verdict, or ``None`` when it does not pin the feature.

    Public because the dashboard's write path needs the same answer: with the env
    pinning this, a config write is inert, and reporting success for an inert
    write is precisely the false-success the switch exists to prevent.

    Reads the CURRENT process's environment. The dashboard and gatewayd normally
    agree — ``env_target_resolver`` hands the backend a copy of the gateway's own
    env and this flag is not a credential, so it is inherited — but an operator
    who exported it into only one of the two would defeat the check. It is a
    best-effort guard against the common case, not a proof.
    """
    raw = os.environ.get(MCP_APPS_ENV_FLAG, "").strip().lower()
    if raw in MCP_APPS_ENV_FALSE:
        return False
    if raw in MCP_APPS_ENV_TRUE:
        return True
    return None


def _mcp_apps_enabled() -> bool:
    """Feature gate for MCP Apps. **Tightest-wins**: any explicit off disables.

    1. ``KIROCREW_MCP_APPS`` off -> disabled. Absolute kill switch.
    2. ``mcp_gateway.apps_enabled = false`` -> disabled, EVEN with the env flag
       on. This is what makes the dashboard opt-out trustworthy across process
       boundaries: a daemon that outlives its gateway keeps its own environment,
       so an env-first precedence let an *adopted* daemon carry on rendering
       after the user had switched the feature off. Deciding it here, in the
       process that actually renders, needs no cross-process probe of anyone's
       environment.
    3. ``KIROCREW_MCP_APPS`` on -> enabled (explicit override for tests and the
       e2e harness), having cleared the opt-out above.
    4. Otherwise ``mcp_gateway.enabled`` — the broker must be running, because
       the render and callback paths live inside it, so ``apps_enabled`` alone
       can never grant the feature.

    ``apps_enabled`` defaults True when absent, so step 2 fires only on a value
    an operator actually wrote: "not configured" is not an opt-out.

    Fails CLOSED: if config cannot be read, the feature is disabled. An
    unreadable config in gatewayd is an abnormal state, and silently disabling
    an optional rendering feature is the low-harm outcome versus rendering
    against an operator preference we could not confirm.

    Read per-call (``KiroCrewConfig.load`` is fingerprint-cached, so this is a
    dict lookup in the common case) so the gateway reflects a config change
    without a daemon restart — including a daemon this gateway merely adopted
    and therefore cannot restart.
    """
    override = mcp_apps_env_override()
    if override is False:
        return False
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        gw = KiroCrewConfig.load().mcp_gateway
    except Exception:  # pragma: no cover - defensive; fail closed
        logger.debug("mcp-apps: config unreadable; treating feature as disabled", exc_info=True)
        return False
    if not gw.apps_enabled:
        return False
    # Deliberately NOT gated on ``gw.enabled``: pooling decides whether backends
    # are shared, not whether the stub exists. The stub carries the app-call
    # relay either way.
    return True


def _inject_client_extensions(msg: dict[str, Any]) -> dict[str, Any]:
    """Return ``msg`` with ``capabilities.extensions["io.modelcontextprotocol/ui"]``
    deep-merged into an ``initialize`` frame — or ``msg`` unchanged when the
    MCP Apps flag is off or the frame is not a well-formed initialize.

    Copy discipline mirrors :func:`_strip_caller_meta`: every dict on the
    mutated path (``params`` → ``capabilities`` → ``extensions``) is shallow-
    copied before mutation so the stub's captured frame is never aliased.
    Pre-existing extension entries are preserved; an existing ui-extension
    entry is left untouched (the caller declared it deliberately).
    """
    if not _mcp_apps_enabled():
        return msg
    params = msg.get("params")
    if not isinstance(params, dict):
        return msg
    out = dict(msg)
    params = dict(params)
    caps_raw = params.get("capabilities")
    caps = dict(caps_raw) if isinstance(caps_raw, dict) else {}
    ext_raw = caps.get("extensions")
    extensions = dict(ext_raw) if isinstance(ext_raw, dict) else {}
    if MCP_APPS_EXTENSION_KEY not in extensions:
        extensions[MCP_APPS_EXTENSION_KEY] = {"mimeTypes": [MCP_APPS_MIME_TYPE]}
    caps["extensions"] = extensions
    params["capabilities"] = caps
    out["params"] = params
    return out


def _inject_caller_meta(msg: dict[str, Any], caller: CallerContext) -> dict[str, Any]:
    """Return a shallow copy of ``msg`` with ``params._meta.kirocrew.caller``
    unconditionally set from ``caller``.

    Assumes any pre-existing stub-supplied caller block has already been
    stripped by :func:`_strip_caller_meta`. Other ``_meta`` fields
    (``progressToken`` etc.) pass through unchanged.
    """
    out = dict(msg)
    params = out.get("params")
    if isinstance(params, dict):
        params = dict(params)
    else:
        params = {}
    meta_raw = params.get("_meta")
    meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
    # Inject the authoritative caller block from the gateway.
    meta.update(build_caller_meta(caller))
    params["_meta"] = meta
    out["params"] = params
    return out


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
    _shutdown_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # --- Sharing boundary state (Milestone 2) -------------------------------
    # Each attached stub appears in ``_stub_inboxes`` keyed by stub_uuid; the
    # inbox is a queue of backend->stub payloads (already-serialised bytes)
    # drained by the connection handler's writer task. ``refcount`` mirrors
    # ``len(_stub_inboxes)`` as a fast read-only integer so the idle-sweep
    # does not have to acquire the inbox lock on every pass.
    _stub_inboxes: dict[str, "asyncio.Queue[bytes]"] = field(default_factory=dict)
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
    # Initialize-cache state — first stub triggers an upstream handshake,
    # later stubs receive a synthesized response built from the cached result.
    _init_result: Optional[dict[str, Any]] = None
    _init_state: str = "unsent"  # "unsent" | "in_flight" | "ready"
    _init_pending: list[tuple[str, Any]] = field(default_factory=list)
    _init_first_stub: Optional[str] = None
    _init_first_id: Any = None
    # Set once the upstream initialize resolves (ready OR failed). The
    # transparent-respawn path (gatewayd) awaits this after re-priming a
    # freshly spawned backend so stub traffic only resumes when the new
    # backend is handshake-complete.
    _init_done_event: asyncio.Event = field(default_factory=asyncio.Event)
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

    async def attach_stub(self, stub_uuid: str) -> "asyncio.Queue[bytes]":
        """Register ``stub_uuid`` as an active consumer of this backend.

        Returns a fresh inbox queue the connection handler must drain. The
        refcount bumps so the idle-sweep skips this backend.
        """
        inbox: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=_STUB_INBOX_MAXSIZE)
        async with self._inbox_lock:
            if stub_uuid in self._stub_inboxes:
                raise RuntimeError(
                    f"stub_uuid={stub_uuid} already attached to backend pid={self.pid}"
                )
            self._stub_inboxes[stub_uuid] = inbox
            self.refcount = len(self._stub_inboxes)
        self.touch()
        logger.debug(
            "attach_stub pool=%s stub=%s refcount=%d",
            self.pool_key.human_readable(), stub_uuid, self.refcount,
        )
        return inbox

    async def detach_stub(self, stub_uuid: str) -> int:
        """Drop ``stub_uuid``'s inbox and clean up any pending requests
        owned by it. Returns the remaining refcount so the caller can
        decide whether to trigger a drain.
        """
        async with self._inbox_lock:
            self._stub_inboxes.pop(stub_uuid, None)
            self.refcount = len(self._stub_inboxes)
        # Drop any pending-request entries owned by the departing stub
        # so the stdout pump does not try to send into a dead inbox.
        stale = [fid for fid, p in self._pending_requests.items() if p.stub_uuid == stub_uuid]
        for fid in stale:
            self._pending_requests.pop(fid, None)
        # Initialize-cache cleanup: if the departing stub was mid-wait for
        # a cached initialize reply, drop it from the pending list.
        self._init_pending = [
            entry for entry in self._init_pending if entry[0] != stub_uuid
        ]
        if self.refcount == 0:
            self.touch()  # start the idle clock fresh
        logger.debug(
            "detach_stub pool=%s stub=%s refcount=%d",
            self.pool_key.human_readable(), stub_uuid, self.refcount,
        )
        return self.refcount

    def _next_forward_id(self) -> str:
        """Return a monotonic gateway-scoped id for rewriting stub requests."""
        self._forward_id_seq += 1
        return f"gw-{self.pid}-{self._forward_id_seq}"

    async def forward_from_stub(
        self,
        stub_uuid: str,
        msg: dict[str, Any],
        *,
        caller: Optional[CallerContext] = None,
    ) -> None:
        """Forward one JSON-RPC message from ``stub_uuid`` to the backend.

        Implements the three pieces of Milestone 2 correctness:

        1. Initialize caching — only the first stub's ``initialize`` reaches
           the backend. Later stubs receive the cached result locally so
           the backend state machine does not see a double-initialize.
        2. Id rewriting — the stub's id is replaced with a gateway-scoped
           monotonic id. The mapping survives in ``_pending_requests`` so
           the stdout pump can put the original id back on the response.
        3. Caller-identity injection — ``tools/call`` requests get a
           ``params._meta.kirocrew.caller`` block when the backend
           advertised the capability at initialize time. The block is
           built via :func:`kiro_crew.mcp_caller.build_caller_meta` so
           gateway + backend share exactly one wire format.
        """
        if not self.is_alive:
            raise BackendGone(self._dead_reason or "backend is not alive")

        method = msg.get("method") if isinstance(msg, dict) else None

        if method == "initialize":
            await self._handle_initialize(stub_uuid, msg)
            return
        if method == "notifications/initialized":
            # ALWAYS suppress stub-originated
            # ``notifications/initialized``. The gateway sends exactly one
            # synthetic notification to the backend from
            # ``_on_upstream_initialize`` once the handshake completes. A stub
            # cannot emit this until it has received its initialize response,
            # which is only delivered AFTER ``_init_state`` is already
            # ``"ready"`` — so the previous "let the first stub through during
            # in_flight" branch was unreachable and the real backend never
            # received the notification at all. A spec-compliant backend that
            # gates tool processing on it would hang forever. Suppressing every
            # stub echo here + one synthetic upstream send preserves the
            # one-initialized-per-backend invariant.
            return

        # Request/response rewrite: only requests carry both method AND id.
        # Pure notifications (method, no id) and pure responses (id, no
        # method) pass through without rewrite. Pure responses are kiro-cli
        # answering a server-to-client request — the backend owns that id
        # table, not us.
        if isinstance(msg, dict):
            orig_id = msg.get("id")
            has_method = "method" in msg
            if has_method and orig_id is not None:
                fid = self._next_forward_id()
                msg = dict(msg)  # shallow copy — we mutate id + maybe _meta
                msg["id"] = fid
                progress_token = None
                tool_name = ""
                tool_arguments = None
                _params = msg.get("params")
                if isinstance(_params, dict):
                    _meta = _params.get("_meta")
                    if isinstance(_meta, dict):
                        progress_token = _meta.get("progressToken")
                    if method == "tools/call":
                        _name = _params.get("name")
                        if isinstance(_name, str):
                            tool_name = _name
                        _args = _params.get("arguments")
                        if isinstance(_args, dict):
                            tool_arguments = _args
                self._pending_requests[fid] = _PendingRequest(
                    stub_uuid=stub_uuid, original_id=orig_id, method=str(method or ""),
                    t_start_ms=time.monotonic() * 1000.0,
                    progress_token=progress_token,
                    session_key=(caller.session_key if caller is not None else ""),
                    tool_name=tool_name,
                    tool_arguments=tool_arguments,
                )
            elif method == "notifications/cancelled":
                # A cancellation is a notification (method, no top-level id);
                # its target lives in params.requestId and still holds the
                # STUB's original id. The backend tracks that request under our
                # gateway-scoped fid, so forwarding verbatim makes the cancel a
                # silent no-op (the tool call keeps running, pinning the shared
                # backend). Remap params.requestId to the fid we assigned this
                # stub's request (scoped to this stub, so no cross-tenant
                # mis-cancel).
                _cparams = msg.get("params")
                if isinstance(_cparams, dict) and "requestId" in _cparams:
                    orig_req = _cparams["requestId"]
                    cancel_fid = next(
                        (f for f, pend in self._pending_requests.items()
                         if pend.stub_uuid == stub_uuid
                         and pend.original_id == orig_req),
                        None,
                    )
                    if cancel_fid is not None:
                        msg = dict(msg)
                        new_params = dict(_cparams)
                        new_params["requestId"] = cancel_fid
                        msg["params"] = new_params
            # Trust boundary: unconditionally strip any stub-supplied caller
            # identity on EVERY forwarded request regardless of method, then
            # inject the authoritative caller block when known.
            msg = _strip_caller_meta(msg)
            if self.supports_caller_identity and caller is not None:
                msg = _inject_caller_meta(msg, caller)

        self.touch()
        try:
            await _write_json_line(self.stdin, msg)
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._dead_reason = f"stdin closed: {exc}"
            raise BackendGone(self._dead_reason) from exc

    async def _handle_initialize(
        self,
        stub_uuid: str,
        msg: dict[str, Any],
    ) -> None:
        original_id = msg.get("id")
        if original_id is None:
            raise ValueError("initialize without id")
        if self._init_state == "ready":
            assert self._init_result is not None
            await self._deliver_cached_initialize(stub_uuid, original_id, self._init_result)
            return
        if self._init_state == "failed":
            raise BackendGone(self._dead_reason or "backend initialize failed")
        if self._init_state == "in_flight":
            self._init_pending.append((stub_uuid, original_id))
            return
        # First time: forward upstream under a gateway id so the stdout pump
        # can route the result to ``_on_upstream_initialize`` instead of the
        # stub (which would still be waiting under the gateway id).
        self._init_state = "in_flight"
        self._init_first_stub = stub_uuid
        self._init_first_id = original_id
        self._init_pending.append((stub_uuid, original_id))
        fid = self._next_forward_id()
        self._pending_requests[fid] = _PendingRequest(
            stub_uuid="__init__", original_id=None, method="initialize",
            t_start_ms=time.monotonic() * 1000.0,
        )
        # Trust boundary: strip any stub-supplied caller identity from the
        # initialize forward too. forward_from_stub strips it on every other
        # forwarded request, but ``initialize`` returns early through this
        # path — without this a stub could forge _meta.kirocrew.caller at init.
        forward_msg = _strip_caller_meta(msg)
        # MCP Apps: advertise the ui extension to the backend (no-op unless
        # the KIROCREW_MCP_APPS flag is on). Must follow the strip so the
        # injected frame is our copy, never the stub's.
        forward_msg = _inject_client_extensions(forward_msg)
        forward_msg["id"] = fid
        self.touch()
        try:
            await _write_json_line(self.stdin, forward_msg)
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._dead_reason = f"stdin closed: {exc}"
            raise BackendGone(self._dead_reason) from exc

    async def _deliver_cached_initialize(
        self,
        stub_uuid: str,
        original_id: Any,
        cached_result: dict[str, Any],
    ) -> None:
        """Synthesize a cached-initialize response and drop it into the stub's
        inbox. The stub sees a reply shaped exactly like one from a real
        backend — same ``result`` object, the stub's own ``id`` restored.
        """
        response = {"jsonrpc": "2.0", "id": original_id, "result": cached_result}
        async with self._inbox_lock:
            inbox = self._stub_inboxes.get(stub_uuid)
        if inbox is not None:
            await self._enqueue_to_stub(
                stub_uuid, inbox,
                (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"),
            )

    async def prime_initialize(
        self,
        init_msg: dict[str, Any],
        *,
        timeout: float = 15.0,
    ) -> None:
        """Re-drive the MCP ``initialize`` handshake on a freshly respawned
        backend using a stub's captured ``initialize`` request, WITHOUT
        delivering any response to a stub.

        Used by the gatewayd bridge's transparent-respawn path: when a shared
        backend dies, a fresh one is spawned and primed here so it reaches
        ``_init_state == "ready"`` before stub traffic resumes. kiro-cli never
        re-sends ``initialize`` after a backend dies, so the gateway must
        replay it on kiro-cli's behalf and swallow the reply.

        Concurrency-safe: if several stubs re-acquire the same fresh backend
        at once, the first drives the upstream handshake and the rest await
        the shared completion event. Raises :class:`BackendGone` if the
        backend is not alive, the handshake fails, or it times out.
        """
        if not self.is_alive:
            raise BackendGone(self._dead_reason or "backend not alive")
        if self._init_state == "ready":
            return
        if self._init_state == "failed":
            raise BackendGone(self._dead_reason or "backend initialize failed")
        if self._init_state == "unsent":
            # We are the first to prime this fresh backend. Forward the
            # captured initialize upstream under a gateway id routed to the
            # ``__init__`` sentinel so the stdout pump feeds the reply to
            # ``_on_upstream_initialize`` (which caches it + sets the done
            # event) rather than to any stub. No ``_init_pending`` entry is
            # added, so no stub ever receives this synthetic reply.
            self._init_state = "in_flight"
            fid = self._next_forward_id()
            self._pending_requests[fid] = _PendingRequest(
                stub_uuid="__init__", original_id=None, method="initialize",
                t_start_ms=time.monotonic() * 1000.0,
            )
            # Strip a stub-forged caller block from the respawn init forward
            # too (mirrors _handle_initialize). captured_init is a shallow copy
            # taken before forward_from_stub's strip, so it can still carry a
            # forged flat CALLER_META_KEY.
            forward_msg = _strip_caller_meta(init_msg)
            # MCP Apps: same injection as _handle_initialize so a respawned
            # backend sees the identical ui capability (flag-gated no-op).
            forward_msg = _inject_client_extensions(forward_msg)
            forward_msg["id"] = fid
            self.touch()
            try:
                await _write_json_line(self.stdin, forward_msg)
            except (BrokenPipeError, ConnectionResetError) as exc:
                self._dead_reason = f"stdin closed: {exc}"
                raise BackendGone(self._dead_reason) from exc
        # Either we just sent it, or another stub's prime is in flight — wait
        # for _on_upstream_initialize / _fail_init to resolve the handshake.
        try:
            await asyncio.wait_for(self._init_done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._dead_reason = self._dead_reason or "initialize timed out on respawn"
            # The backend answered neither the handshake nor EOF within the
            # window: it is wedged (process alive, initialize never completing).
            # Mark init failed + wake any co-primer so they fail fast instead of
            # each waiting the full timeout, and reap the wedged process now
            # rather than leaving it marked-dead-but-running until the idle
            # sweep reclaims it. shutdown() is idempotent and self-contained —
            # it kills the process group but touches no pool bookkeeping, so
            # there is no reserve/evict race with a concurrent acquirer.
            self._init_state = "failed"
            self._init_done_event.set()
            with contextlib.suppress(Exception):
                await self.shutdown()
            raise BackendGone(self._dead_reason) from exc
        if self._init_state != "ready":
            raise BackendGone(
                self._dead_reason or "backend initialize failed on respawn"
            )

    async def _broadcast_backend_gone(self, reason: str) -> None:
        """Send a synthetic JSON-RPC error to every attached stub before
        closing. Preserves the existing correlation between in-flight ids
        and stubs: each pending request gets its own error reply.
        """
        # Idempotent: reachable near-simultaneously from the stdout-pump
        # finally, _heartbeat_once, _fail_oversize_request and the init-fail
        # paths. Without this guard each invocation snapshots pending/init and
        # double-delivers error replies to every stub. The check+set has no
        # await between the two statements, so it is atomic on the event loop.
        if self._gone_broadcast:
            return
        self._gone_broadcast = True
        # Fast-fail any in-flight prime_initialize() waiter. If the backend
        # dies mid-handshake (stdout EOF before it answered initialize),
        # neither _on_upstream_initialize nor _fail_init fires, so a
        # transparent-respawn primer awaiting _init_done_event would otherwise
        # block for the full timeout before raising BackendGone. Mark init
        # failed and wake the waiter immediately.
        if self._init_state == "in_flight":
            self._init_state = "failed"
            self._dead_reason = self._dead_reason or reason
            self._init_done_event.set()
        async with self._inbox_lock:
            inboxes = dict(self._stub_inboxes)
        for fid, pending in list(self._pending_requests.items()):
            if pending.stub_uuid == _APPS_STUB_SENTINEL:
                # Wake a parked ui:// fetch immediately so its coroutine falls
                # back to delivering the original response rather than blocking
                # for the full resources/read timeout after the backend died.
                fut = pending.apps_future
                if fut is not None and not fut.done():
                    fut.set_exception(BackendGone(f"backend gone: {reason}"))
                continue
            if pending.stub_uuid == "__init__":
                # Each queued initialize waiter gets its own rejection so
                # the originating stub sees an error against its own id.
                for stub_uuid, original_id in self._init_pending:
                    inbox = inboxes.get(stub_uuid)
                    if inbox is None:
                        continue
                    err = {
                        "jsonrpc": "2.0",
                        "id": original_id,
                        "error": {"code": -32000, "message": f"backend gone: {reason}"},
                    }
                    await self._enqueue_to_stub(
                        stub_uuid,
                        inbox,
                        (json.dumps(err, separators=(",", ":")) + "\n").encode("utf-8"),
                    )
                continue
            inbox = inboxes.get(pending.stub_uuid)
            if inbox is None:
                continue
            err = {
                "jsonrpc": "2.0",
                "id": pending.original_id,
                "error": {"code": -32000, "message": f"backend gone: {reason}"},
            }
            await self._enqueue_to_stub(
                pending.stub_uuid, inbox,
                (json.dumps(err, separators=(",", ":")) + "\n").encode("utf-8"),
            )
        self._pending_requests.clear()
        self._init_pending.clear()

    async def run_stdout_pump(self) -> None:
        """Read backend stdout line-by-line and route each line back to the
        originating stub. Exits on EOF (backend crash or clean exit). Never
        wrapped in a timeout — the learned correction explicitly warns
        against that pattern because it kills healthy long-lived sessions.
        """
        try:
            while True:
                try:
                    line = await self.stdout.readuntil(b"\n")
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        logger.warning(
                            "backend pid=%s closed stdout mid-line (%d bytes)",
                            self.pid, len(exc.partial),
                        )
                    break
                except asyncio.LimitOverrunError:
                    # Drain the oversize line up to AND
                    # INCLUDING its terminating newline WITHOUT consuming bytes
                    # of the following frame. ``readuntil`` stops exactly at the
                    # newline and leaves the remainder buffered; while the
                    # not-yet-terminated prefix still exceeds the limit it
                    # re-raises LimitOverrunError, so we consume that prefix
                    # (``exc.consumed``) and retry. The previous ``read(8192)``
                    # drain discarded post-newline bytes of the next response,
                    # hanging the next request. The reader ``limit`` is
                    # ``READ_BUFFER_LIMIT_BYTES`` (1 MiB); a longer line is
                    # pathological and dropped.
                    # Keep only the first _OVERSIZE_KEEP bytes — enough for
                    # _fail_oversize_request to parse the JSON-RPC id — while
                    # still draining the whole line off the pipe. Accumulating
                    # the entire (possibly multi-GB) line would itself be the
                    # memory blow-up this guard exists to prevent.
                    _OVERSIZE_KEEP = 512
                    oversize_head = b""
                    try:
                        while True:
                            try:
                                tail = await self.stdout.readuntil(b"\n")
                                if len(oversize_head) < _OVERSIZE_KEEP:
                                    oversize_head += tail[:_OVERSIZE_KEEP - len(oversize_head)]
                                break
                            except asyncio.LimitOverrunError as exc:
                                chunk = await self.stdout.readexactly(exc.consumed)
                                if len(oversize_head) < _OVERSIZE_KEEP:
                                    oversize_head += chunk[:_OVERSIZE_KEEP - len(oversize_head)]
                    except (asyncio.IncompleteReadError, Exception):  # noqa: BLE001
                        pass
                    logger.warning(
                        "backend pid=%s dropped oversize stdout line (>%d bytes)",
                        self.pid, READ_BUFFER_LIMIT_BYTES,
                    )
                    # Fail the pending request so the waiting stub is not left
                    # dangling. Without this the heartbeat eventually kills the
                    # shared backend for ALL co-pooled sessions.
                    await self._fail_oversize_request(oversize_head)
                    continue
                if not line:
                    break
                # Spill oversized (but under the read limit) responses to a
                # sidecar file and truncate inline, so a large-but-legitimate
                # tool result doesn't balloon the shared daemon's memory or the
                # agent's context. Offloaded to the maintenance executor (short
                # filesystem I/O); a spill failure falls back to the raw line.
                if len(line) > RESPONSE_SPILL_THRESHOLD_BYTES:
                    try:
                        line = await asyncio.get_running_loop().run_in_executor(
                            maintenance_executor(),
                            maybe_spill_response,
                            line,
                            self.pool_key.server_name,
                            RESPONSE_SPILL_THRESHOLD_BYTES,
                        )
                    except Exception:
                        logger.debug("spill-to-file failed; routing raw line", exc_info=True)
                await self._route_backend_line(line)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover — defensive
            logger.exception("backend stdout pump crashed pid=%s", self.pid)
        finally:
            reason = self._dead_reason or (
                f"exit rc={self.process.returncode}"
                if self.process.returncode is not None
                else "stdout EOF"
            )
            self._dead_reason = reason
            await self._broadcast_backend_gone(reason)

    async def _route_backend_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.debug("backend non-JSON stdout line dropped: %r", line[:200])
            return
        if not isinstance(msg, dict):
            return
        msg_id = msg.get("id")
        method = msg.get("method")
        if msg_id is not None and method is None:
            # Gateway-internal liveness pong: the heartbeat
            # ping is sent under HEARTBEAT_PING_ID and its reply (result or
            # error) is consumed here, never routed to a stub.
            if _is_heartbeat_id(msg_id):
                self._last_ping_response_mono = time.monotonic()
                return
            # Response to a previously-forwarded request.
            pending = self._pending_requests.pop(str(msg_id), None)
            if pending is None:
                logger.warning(
                    "backend pid=%s response to unknown id=%r; dropping",
                    self.pid, msg_id,
                )
                return
            if pending.t_start_ms:
                # Fire-and-forget: awaiting the emit here (even with its file
                # I/O offloaded to a thread) yields the shared stdout pump,
                # adding head-of-line latency to co-pooled sessions whenever
                # the metrics volume is slow. Schedule it off the hot path.
                self._spawn_metric_task({
                    "ts": int(time.time() * 1000),
                    "method": pending.method,
                    "dur_ms": round(time.monotonic() * 1000.0 - pending.t_start_ms, 3),
                    "pool": self.pool_key.human_readable(),
                    "pid": self.pid,
                    "ok": "error" not in msg,
                    "stub": pending.stub_uuid,
                })
            if pending.stub_uuid == "__init__":
                await self._on_upstream_initialize(msg)
                return
            if pending.stub_uuid == _APPS_STUB_SENTINEL:
                # Gateway-originated resources/read reply for an MCP Apps
                # ui:// fetch — hand it to the parked fetch coroutine, never a
                # stub. (Already popped above so it is removed exactly once.)
                fut = pending.apps_future
                if fut is not None and not fut.done():
                    fut.set_result(msg)
                return
            # MCP Apps interception: a tools/call result carrying a ui://
            # resource is parked (the response is held off the stub) while an
            # out-of-band resources/read fetches the app payload. When it
            # returns True the (marked) response is delivered asynchronously by
            # a background task, so DO NOT deliver here.
            if await self._maybe_intercept_ui_result(pending, msg):
                self.touch()
                return
            rewritten = dict(msg)
            rewritten["id"] = pending.original_id
            await self._deliver_to_stub(pending.stub_uuid, rewritten)
            self.touch()
            return
        if method is not None and msg_id is None:
            # Attribute request-scoped notifications (progress, or a log tied
            # to an in-flight call) to their owning stub so they are not leaked
            # to co-pooled tenants sharing this backend.
            owner = self._notification_owner(msg)
            if owner is not None:
                await self._deliver_to_stub(owner, msg)
            elif method in _GLOBAL_BROADCAST_NOTIFICATIONS:
                # Genuinely backend-wide state (identical for every tenant,
                # e.g. tools/list_changed) — safe to fan out to all stubs.
                await self._broadcast(msg)
            else:
                # Unattributable request-scoped notification (progress/logging
                # without a unique routing token, or a token that collided
                # across tenants). Broadcasting it would disclose one tenant's
                # request-scoped content to co-tenants — a leak the non-pooled
                # baseline never had — so drop it (deny-by-default) rather than
                # guess an owner.
                logger.debug(
                    "backend pid=%s dropping unattributable request-scoped "
                    "notification %r (not broadcast to avoid cross-tenant leak)",
                    self.pid, method,
                )
            return
        # Server-to-client request (has method AND id) — route ONLY when we can
        # attribute it unambiguously:
        # 1. _meta.relatedRequestId -> owning stub (MCP-spec aligned)
        # 2. Single attached stub -> trivial case
        # Otherwise (multiple stubs, no relatedRequestId) we recycle the backend
        # rather than guess, to avoid a cross-tenant leak.
        if method is not None and msg_id is not None:
            target_stub: Optional[str] = None
            # Priority 1: _meta.relatedRequestId lookup
            params = msg.get("params")
            if isinstance(params, dict):
                meta = params.get("_meta")
                if isinstance(meta, dict):
                    related_id = meta.get("relatedRequestId")
                    if related_id is not None:
                        pending = self._pending_requests.get(str(related_id))
                        if pending is not None:
                            target_stub = pending.stub_uuid
            # Priority 2: single stub attached
            if target_stub is None:
                async with self._inbox_lock:
                    stubs = list(self._stub_inboxes.keys())
                if len(stubs) == 1:
                    target_stub = stubs[0]
            # Priority 2 (single stub) is the only safe fallback: with multiple
            # stubs and no relatedRequestId we cannot attribute a server->client
            # request to a tenant without risking a cross-tenant leak
            # (delivering B's sampling/elicitation to A, or broadcasting a
            # request every tenant would answer). Refuse to keep pooling: recycle
            # the backend so its stubs fall back to an unambiguous per-session
            # exec. (Well-behaved servers set relatedRequestId -> Priority 1.)
            if target_stub is not None:
                await self._deliver_to_stub(target_stub, msg)
            else:
                async with self._inbox_lock:
                    nstubs = len(self._stub_inboxes)
                reason = (
                    "server-initiated request without relatedRequestId while "
                    f"{nstubs} stubs share this backend; cannot route without a "
                    "cross-tenant leak — recycling"
                )
                logger.warning("backend pid=%s %s", self.pid, reason)
                self._dead_reason = self._dead_reason or reason
                await self._broadcast_backend_gone(reason)
            return
        logger.debug("backend pid=%s emitted malformed JSON-RPC: %r", self.pid, msg)

    async def _fail_init(self, reason: str) -> None:
        """Transition init to the terminal ``"failed"`` state and flush every
        queued waiter with an explicit JSON-RPC error.

        Called from :meth:`_on_upstream_initialize` when the backend's reply
        is a JSON-RPC error or a malformed result. Without this path, stubs
        queued in ``_init_pending`` during the in-flight window would hang
        forever, and future stubs would keep piling into ``_init_pending``
        because ``is_alive`` is still True.
        """
        self._init_state = "failed"
        self._dead_reason = self._dead_reason or f"init failed: {reason}"
        self._init_done_event.set()
        logger.error("backend pid=%s %s", self.pid, self._dead_reason)
        pending = list(self._init_pending)
        self._init_pending.clear()
        async with self._inbox_lock:
            inboxes = dict(self._stub_inboxes)
        for stub_uuid, original_id in pending:
            inbox = inboxes.get(stub_uuid)
            if inbox is None:
                continue
            err = {
                "jsonrpc": "2.0",
                "id": original_id,
                "error": {"code": -32000, "message": f"backend init failed: {reason}"},
            }
            await self._enqueue_to_stub(
                stub_uuid, inbox,
                (json.dumps(err, separators=(",", ":")) + "\n").encode("utf-8"),
            )

    async def _on_upstream_initialize(self, response: dict[str, Any]) -> None:
        """Process the backend's reply to the first stub's ``initialize``.

        Caches the ``result`` so later stubs can be served locally; detects
        the caller-identity capability; flushes every queued stub.

        On error (backend returned a JSON-RPC error, or a malformed result),
        transitions to the terminal ``"failed"`` state and flushes all
        queued stubs with an explicit error response. Without this a stub
        that registered during the in-flight window would hang forever
        waiting for a cached-initialize that never arrives.
        """
        if "error" in response:
            await self._fail_init(f"initialize error: {response['error']}")
            return
        result = response.get("result")
        if not isinstance(result, dict):
            await self._fail_init(
                f"initialize response missing/malformed result: {response!r}"
            )
            return
        self._init_result = result
        self._init_state = "ready"
        self._init_done_event.set()
        capabilities = result.get("capabilities") or {}
        experimental = capabilities.get("experimental") or {}
        self.supports_caller_identity = isinstance(experimental, dict) and (
            CALLER_CAPABILITY_KEY in experimental
        )
        logger.info(
            "backend pid=%s initialized supports_caller_identity=%s",
            self.pid, self.supports_caller_identity,
        )
        # Forward exactly one synthetic
        # notifications/initialized to the backend now the handshake is
        # complete. Stub-originated copies are always suppressed upstream, so
        # without this a backend that gates tool processing on the
        # notification would never receive it and would hang.
        try:
            await _write_json_line(
                self.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
        except (BrokenPipeError, ConnectionResetError) as exc:  # pragma: no cover
            self._dead_reason = f"stdin closed during initialized: {exc}"
        pending = list(self._init_pending)
        self._init_pending.clear()
        for stub_uuid, original_id in pending:
            await self._deliver_cached_initialize(stub_uuid, original_id, result)

    async def _enqueue_to_stub(
        self, stub_uuid: str, inbox: "asyncio.Queue[bytes]", data: bytes
    ) -> bool:
        """Non-blocking enqueue into a stub's inbox.

        Returns ``True`` on success. If the inbox is full — the stub has
        stopped draining its socket — the stub is dropped via
        :meth:`detach_stub` and ``False`` returned. A wedged stub must never
        apply backpressure to the shared stdout pump nor let a chatty backend
        grow gateway RSS without bound; dropping the one slow stub protects
        every co-pooled session.
        """
        try:
            inbox.put_nowait(data)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "backend pid=%s stub=%s inbox full (cap=%d); dropping slow stub",
                self.pid, stub_uuid, _STUB_INBOX_MAXSIZE,
            )
            await self.detach_stub(stub_uuid)
            return False

    async def _deliver_to_stub(self, stub_uuid: str, msg: dict[str, Any]) -> None:
        async with self._inbox_lock:
            inbox = self._stub_inboxes.get(stub_uuid)
        if inbox is None:
            logger.debug(
                "backend pid=%s response for detached stub=%s; dropping",
                self.pid, stub_uuid,
            )
            return
        await self._enqueue_to_stub(
            stub_uuid, inbox, (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        )

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        # FIXED: Server-to-client requests now route via the
        # priority chain (relatedRequestId -> single-stub -> last-requester)
        # before falling back here. Broadcast is only used for notifications
        # and as a last-resort fallback when no stub can be identified.
        payload = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._inbox_lock:
            inboxes = list(self._stub_inboxes.items())
        for stub_uuid, inbox in inboxes:
            await self._enqueue_to_stub(stub_uuid, inbox, payload)

    def _notification_owner(self, msg: dict[str, Any]) -> Optional[str]:
        """Best-effort attribution of a server->client notification to the one
        stub that owns the originating request, so a request-scoped
        notification (progress, or a log tied to an in-flight call) is not
        leaked to co-pooled tenants. Returns None for unattributable /
        genuinely global notifications, which the caller broadcasts.

        progressToken collisions across tenants are possible (clients pick
        their own), so only a UNIQUELY-owned token routes; an ambiguous token
        falls through to broadcast (no worse than the pre-scoping behaviour)."""
        params = msg.get("params")
        if not isinstance(params, dict):
            return None
        # progress notifications echo the request's progressToken.
        token = params.get("progressToken")
        if token is not None:
            owners = {
                p.stub_uuid
                for p in self._pending_requests.values()
                if p.progress_token == token and p.stub_uuid != "__init__"
            }
            if len(owners) == 1:
                return next(iter(owners))
        # logging / other notifications may carry _meta.relatedRequestId.
        meta = params.get("_meta")
        if isinstance(meta, dict):
            related_id = meta.get("relatedRequestId")
            if related_id is not None:
                pending = self._pending_requests.get(str(related_id))
                if pending is not None and pending.stub_uuid != "__init__":
                    return pending.stub_uuid
        return None

    async def _maybe_intercept_ui_result(
        self, pending: _PendingRequest, msg: dict[str, Any]
    ) -> bool:
        """Decide whether ``msg`` (a completed tools/call response) carries an
        MCP Apps ui:// resource that must be fetched + spooled before delivery.

        Returns ``True`` when interception was *initiated* — a background task
        now owns delivering the (marked) response to the stub, so the caller
        must NOT deliver it. Returns ``False`` (the common/off path) when the
        caller should deliver the response normally right now. Never raises:
        any classification hiccup falls back to normal delivery.
        """
        if not _mcp_apps_enabled():
            return False
        # App-originated callbacks (the app-call relay forwards a tools/call on
        # a ``__app_call__*`` stub) must NEVER be re-intercepted: if the called
        # tool itself declares a ui:// resource, re-spooling would replace the
        # app's real result with an internal marker string and mint a stray
        # spool record. The render/spool path is only for MODEL-originated tool
        # results; app callbacks return verbatim to the requesting app.
        if pending.stub_uuid.startswith("__app_call__"):
            return False
        # Passive harvest: tool declarations are the SEP-1865 PRIMARY place a
        # server associates a tool with its ui:// resource (the real
        # pdf-server and Excalidraw declare it ONLY here, not on results).
        # Every tools/list response that flows through updates the map.
        if pending.method == "tools/list":
            result = msg.get("result")
            if isinstance(result, dict):
                try:
                    declared = extract_declared_ui_uris(result)
                except Exception:  # pragma: no cover — defensive; extract is total
                    declared = {}
                # REPLACE (never merge): each tools/list is the server's
                # complete current declaration set — merging would keep a
                # WITHDRAWN tool→ui association alive until backend restart,
                # so later successful calls would still render the withdrawn
                # app resource.
                self._apps_declared_uris = declared
            return False
        if pending.method != "tools/call":
            return False
        result = msg.get("result")
        if not isinstance(result, dict):
            return False
        if result.get("isError"):
            # A FAILED tool call must never spawn an app render (nor mint a
            # live spool capability) — checked before EITHER association form
            # (result-side _meta.ui or the tools/list declaration) is read.
            return False
        try:
            resource_uri = extract_ui_resource_uri(result)
        except Exception:  # pragma: no cover — defensive; extract is total
            logger.debug("mcp-apps: extract_ui_resource_uri raised", exc_info=True)
            return False
        if resource_uri is None:
            # Fall back to the uri the tool DECLARED in tools/list (the
            # SEP-1865 primary form real servers use).
            resource_uri = self._apps_declared_uris.get(pending.tool_name)
        if resource_uri is None:
            return False
        task = asyncio.create_task(
            self._fetch_and_deliver_ui(pending, msg, resource_uri)
        )
        self._apps_tasks.add(task)
        task.add_done_callback(self._apps_tasks.discard)
        return True

    async def _fetch_and_deliver_ui(
        self, pending: _PendingRequest, msg: dict[str, Any], resource_uri: str
    ) -> None:
        """Out-of-band fetch of a ui:// resource, spool it, mark the tools/call
        response, and deliver the (possibly-marked) response to the stub.

        On ANY failure/timeout the ORIGINAL response is delivered unmodified
        and a warning logged — the app render is best-effort and must never
        wedge or drop the tool result. Runs as a background task so the shared
        stdout pump is never blocked awaiting the resources/read round-trip.
        """
        response = dict(msg)
        response["id"] = pending.original_id
        try:
            contents = await self._read_ui_resource(resource_uri)
            html, csp, permissions = self._parse_ui_contents(contents)
            result = msg.get("result")
            structured = result.get("structuredContent") if isinstance(result, dict) else None
            content = result.get("content") if isinstance(result, dict) else None
            spool_id = await asyncio.to_thread(write_spool, {
                "server": self.pool_key.server_name,
                "tool": pending.tool_name,
                "session_key": pending.session_key,
                # Exact-identity binding for the app→gateway callback: the
                # callback resolves its backend EXCLUSIVELY by this digest, so
                # an app can only ever call back into the same pool partition
                # (same credentials/sandbox/approval identity) that produced
                # it — never a co-pooled tenant's backend for the same server.
                "pool_digest": self.storage_digest,
                "html": html,
                "csp": csp,
                "permissions": permissions,
                "structured_content": structured,
                # Originating tools/call inputs + full result content, so the
                # app initializes from its REAL state (SEP-1865 tool-input /
                # tool-result notifications) instead of empty placeholders.
                "tool_input": pending.tool_arguments,
                "result_content": content if isinstance(content, list) else None,
            })
            if isinstance(result, dict):
                response["result"] = append_marker(result, spool_id)
            logger.info(
                "mcp-apps: spooled ui resource %s for tool=%s server=%s id=%s",
                resource_uri, pending.tool_name or "?",
                self.pool_key.server_name, spool_id,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; deliver original
            logger.warning(
                "mcp-apps: ui fetch/spool failed for %s (tool=%s); delivering "
                "original response unmodified: %s",
                resource_uri, pending.tool_name or "?", exc,
            )
            response = dict(msg)
            response["id"] = pending.original_id
        await self._deliver_to_stub(pending.stub_uuid, response)

    async def _read_ui_resource(self, resource_uri: str) -> list[Any]:
        """Issue a gateway-originated ``resources/read`` for ``resource_uri`` to
        this backend and return its ``result.contents`` list.

        Parks a future under a ``_APPS_STUB_SENTINEL`` pending entry (same
        gateway-id mechanism the initialize handshake uses) so the stdout pump
        resolves it. Bounded by :data:`_APPS_RESOURCE_READ_TIMEOUT_SECS`.
        """
        fid = self._next_forward_id()
        fut: "asyncio.Future[dict[str, Any]]" = asyncio.get_running_loop().create_future()
        self._pending_requests[fid] = _PendingRequest(
            stub_uuid=_APPS_STUB_SENTINEL, original_id=None, method="resources/read",
            t_start_ms=time.monotonic() * 1000.0, apps_future=fut,
        )
        request = {
            "jsonrpc": "2.0",
            "id": fid,
            "method": "resources/read",
            "params": {"uri": resource_uri},
        }
        try:
            await _write_json_line(self.stdin, request)
            response = await asyncio.wait_for(fut, timeout=_APPS_RESOURCE_READ_TIMEOUT_SECS)
        finally:
            # Resolved path already popped it in _route_backend_line; this
            # covers the timeout/write-failure path so no stale entry lingers.
            self._pending_requests.pop(fid, None)
        if "error" in response:
            raise RuntimeError(f"resources/read error: {response['error']}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"resources/read malformed result: {response!r}")
        contents = result.get("contents")
        if not isinstance(contents, list) or not contents:
            raise RuntimeError("resources/read returned no contents")
        return contents

    def _parse_ui_contents(self, contents: list[Any]) -> tuple[str, Any, Any]:
        """Extract ``(html, csp, permissions)`` from a resources/read
        ``contents`` list. Requires ``contents[0].mimeType`` to equal
        :data:`MCP_APPS_MIME_TYPE`; reads inline ``text`` or base64 ``blob``;
        pulls ``csp``/``permissions`` from ``contents[0]._meta.ui``."""
        first = contents[0]
        if not isinstance(first, dict):
            raise RuntimeError("resources/read contents[0] is not an object")
        mime = first.get("mimeType")
        if mime != MCP_APPS_MIME_TYPE:
            raise RuntimeError(
                f"unexpected mimeType {mime!r} (want {MCP_APPS_MIME_TYPE!r})"
            )
        text = first.get("text")
        if isinstance(text, str):
            html = text
        elif isinstance(first.get("blob"), str):
            try:
                html = base64.b64decode(first["blob"], validate=True).decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
                raise RuntimeError(f"invalid base64 blob: {exc}") from exc
        else:
            raise RuntimeError("resources/read contents[0] has neither text nor blob")
        meta = first.get("_meta")
        ui = meta.get("ui") if isinstance(meta, dict) else None
        if not isinstance(ui, dict):
            ui = {}
        return html, ui.get("csp"), ui.get("permissions")

    def _spawn_metric_task(self, record: dict[str, Any]) -> None:
        """Schedule a best-effort latency-metric emit off the stdout-pump
        critical path. No-op when metrics are disabled (the default), so the
        shared pump does not allocate + schedule + discard a Task per RPC
        response for every co-pooled tenant. Tracked in ``_metric_tasks``
        (discarded on completion) so the task isn't GC'd before it runs; a slow
        metrics disk therefore cannot back-pressure frame routing."""
        if _METRICS_PATH is None:
            return
        task = asyncio.create_task(_emit_call_metric(record))
        self._metric_tasks.add(task)
        task.add_done_callback(self._metric_tasks.discard)

    async def _fail_oversize_request(self, raw: bytes) -> None:
        """Try to extract the JSON-RPC id from an oversize response and fail
        just that request, so the waiting stub is unblocked without killing
        the entire shared backend.

        Best-effort: if the id cannot be parsed (e.g. the id field is beyond
        the buffer we captured), fall back to failing the most-recent pending
        request — at worst one stub gets an error, but the backend stays alive
        for all others.
        """
        msg_id: Any = None
        # Attempt to parse the id from the beginning of the oversize line.
        try:
            # The first ~200 bytes should contain {"jsonrpc":"2.0","id":...
            prefix = raw[:512].decode("utf-8", errors="replace")
            partial = json.loads(prefix.split("\n", 1)[0]) if prefix.strip().endswith("}") else None
            if isinstance(partial, dict):
                msg_id = partial.get("id")
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            pass
        # If prefix-parse failed, try a targeted regex for "id": value.
        if msg_id is None:
            prefix_str = raw[:256].decode("utf-8", errors="replace")
            m = re.search(r'"id"\s*:\s*("(?:[^"\\]|\\.)*?"|\d+|null)', prefix_str)
            if m:
                try:
                    msg_id = json.loads(m.group(1))
                except (json.JSONDecodeError, ValueError):
                    pass
        if msg_id is not None:
            pending = self._pending_requests.pop(str(msg_id), None)
            if pending is not None and pending.stub_uuid == "__init__":
                # Oversize *initialize* response: failing one request is not
                # enough — the handshake can never complete, so ``_init_state``
                # is stuck "in_flight" and every queued stub (plus any
                # prime_initialize waiter) hangs forever with no wedge the
                # heartbeat can detect. Recycle the whole backend instead so
                # every init waiter gets a clean BackendGone and re-establishes
                # (_broadcast_backend_gone marks init failed + wakes the event).
                reason = (
                    f"oversize initialize response (>{READ_BUFFER_LIMIT_BYTES} "
                    "bytes); recycling shared backend"
                )
                self._dead_reason = self._dead_reason or reason
                await self._broadcast_backend_gone(reason)
                return
            if pending is not None:
                err_response = {
                    "jsonrpc": "2.0",
                    "id": pending.original_id,
                    "error": {
                        "code": -32000,
                        "message": (
                            f"response exceeded size limit "
                            f"({READ_BUFFER_LIMIT_BYTES} bytes); request dropped"
                        ),
                    },
                }
                await self._deliver_to_stub(pending.stub_uuid, err_response)
            return
        # Id unrecoverable (it sat past the captured prefix): do NOT fail an
        # arbitrary pending request — that sends a spurious error to an innocent
        # stub while the real culprit keeps hanging until the wedge timeout.
        # Recycle the shared backend instead so every attached stub gets a clean
        # BackendGone and re-establishes.
        reason = (
            f"oversize response (>{READ_BUFFER_LIMIT_BYTES} bytes) with "
            "unrecoverable request id; recycling shared backend"
        )
        self._dead_reason = self._dead_reason or reason
        await self._broadcast_backend_gone(reason)

    async def _heartbeat_once(self, now: float) -> str:
        """Classify backend liveness for one heartbeat tick and recover a
        wedged backend in place.

        Returns one of:

        * ``"gone"``   -- the OS has already reaped the subprocess
          (``process.returncode is not None``), or a liveness ping write hit
          a broken pipe. Marked dead; every attached stub receives a
          synthetic error via :meth:`_broadcast_backend_gone`.
        * ``"idle"``   -- no stubs attached (``refcount == 0``). LEFT ALONE:
          the idle-sweep owns eviction of these on its own timer. Recycling
          idle-but-healthy backends here would re-introduce the cr-guide
          over-reaping regression (MCPool 0.2.7).
        * ``"wedged"`` -- a stub is attached AND BOTH: (1) an in-flight request
          exceeds :data:`HEARTBEAT_TIMEOUT_SECS`, AND (2) no ping response has
          arrived within :data:`PING_STALE_SECS` (backend unresponsive). OR the
          hard ceiling :data:`HARD_WEDGE_CEILING_SECS` is exceeded regardless
          of ping freshness. Shared backends (kirocrew-core) host long tools
          like ``wait`` (60-1800s) and ``spawn_sub_agents``; recycling them on
          request age alone kills healthy-but-slow backends.
        * ``"alive"``  -- everything else. A best-effort JSON-RPC ``ping`` is
          written under the reserved :data:`HEARTBEAT_PING_ID`; the response is
          swallowed in :meth:`_route_backend_line`. A failed ping write
          (broken pipe) is itself a liveness failure and downgrades to
          ``"gone"``.
        """
        # 1. Process already reaped by the OS.
        if self.process.returncode is not None:
            if self._dead_reason is None:
                self._dead_reason = f"process exited rc={self.process.returncode}"
            await self._broadcast_backend_gone(self._dead_reason)
            return "gone"

        # 2. No consumers -- leave idle backends to the idle-sweep.
        if self.refcount == 0:
            return "idle"

        # 3. Wedge detection: two-condition rule + hard ceiling.
        oldest_age = 0.0
        oldest_fid: Optional[str] = None
        oldest_pending: Optional[_PendingRequest] = None
        for fid, pending in self._pending_requests.items():
            age = now - (pending.t_start_ms / 1000.0)
            if age > oldest_age:
                oldest_age = age
                oldest_fid = fid
                oldest_pending = pending

        if self._pending_requests and oldest_age >= HEARTBEAT_TIMEOUT_SECS:
            ping_age = now - self._last_ping_response_mono
            ping_stale = ping_age >= PING_STALE_SECS

            # Hard ceiling: recycle regardless of ping freshness (pathological)
            if oldest_age >= HARD_WEDGE_CEILING_SECS:
                self._dead_reason = (
                    f"wedged: in-flight request outstanding {oldest_age:.1f}s "
                    f">= {HARD_WEDGE_CEILING_SECS:.0f}s hard ceiling "
                    f"(ping_age={ping_age:.1f}s)"
                )
                logger.warning(
                    "backend pid=%s pool=%s %s; recycling",
                    self.pid, self.pool_key.human_readable(), self._dead_reason,
                )
                await self._broadcast_backend_gone(self._dead_reason)
                return "wedged"

            # Two-condition: old request + stale ping -> wedged
            if ping_stale:
                self._dead_reason = (
                    f"wedged: in-flight request outstanding {oldest_age:.1f}s "
                    f">= {HEARTBEAT_TIMEOUT_SECS:.0f}s timeout AND "
                    f"ping stale {ping_age:.1f}s >= {PING_STALE_SECS:.0f}s"
                )
                logger.warning(
                    "backend pid=%s pool=%s %s; recycling",
                    self.pid, self.pool_key.human_readable(), self._dead_reason,
                )
                await self._broadcast_backend_gone(self._dead_reason)
                return "wedged"

            # Old request + fresh pings: backend responsive but tool slow.
            # Log once per request id, don't recycle.
            if oldest_fid and oldest_fid not in self._warned_slow_ids:
                self._warned_slow_ids.add(oldest_fid)
                logger.warning(
                    "slow in-flight request %.1fs (method=%s, stub=%s, fid=%s) "
                    "but backend pid=%s responsive (ping_age=%.1fs); not recycling",
                    oldest_age,
                    oldest_pending.method if oldest_pending else "?",
                    oldest_pending.stub_uuid if oldest_pending else "?",
                    oldest_fid,
                    self.pid,
                    ping_age,
                )

        # Prune warned ids for completed requests
        if self._warned_slow_ids:
            self._warned_slow_ids &= set(self._pending_requests.keys())

        # 4. Alive: probe with a reserved-id ping.
        try:
            await _write_json_line(
                self.stdin,
                {"jsonrpc": "2.0", "id": HEARTBEAT_PING_ID, "method": "ping"},
            )
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._dead_reason = f"heartbeat ping write failed: {exc}"
            await self._broadcast_backend_gone(self._dead_reason)
            return "gone"
        return "alive"

    async def _cancel_background_tasks(self) -> None:
        """Cancel and await the stdout + stderr pump tasks.

        The stderr pump must be awaited on shutdown; left fire-and-forget it
        keeps running and leaks its stderr pipe fd whenever the
        process outlives SIGKILL — across LRU-eviction churn this exhausts fds.
        """
        for attr in ("_stdout_task", "_stderr_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                setattr(self, attr, None)

    async def cancel_in_flight_for_stub(self, stub_uuid: str) -> list[str]:
        """Send MCP ``notifications/cancelled`` for every in-flight request
        owned by ``stub_uuid``. Returns the list of cancelled forward-ids.

        This is the core Scope-A fix: when a stub disconnects (session killed),
        the backend receives explicit cancellation so it can abort long-running
        tool work instead of running to completion with no consumer.
        """
        # Collect in-flight requests for this stub (before detach clears them)
        in_flight = [
            (fid, p) for fid, p in self._pending_requests.items()
            if p.stub_uuid == stub_uuid
        ]
        cancelled_ids: list[str] = []
        for fid, pending in in_flight:
            if not self.is_alive:
                break
            cancel_notification = {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {
                    "requestId": fid,
                    "reason": "Session stopped — caller disconnected",
                },
            }
            try:
                await _write_json_line(self.stdin, cancel_notification)
                cancelled_ids.append(fid)
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Backend already dead — no point sending more
                break
        if cancelled_ids:
            logger.info(
                "backend pid=%s: sent %d cancel notifications for stub=%s [ids: %s]",
                self.pid, len(cancelled_ids), stub_uuid,
                ", ".join(cancelled_ids[:5]) + ("..." if len(cancelled_ids) > 5 else ""),
            )
        return cancelled_ids

    async def recycle_if_idle(self) -> bool:
        """Kill this backend if refcount == 0 (Scope B fallback).

        The kill is immediate; the *respawn* is lazy — the next pool
        ``get_or_create`` for this key sees a dead entry and spawns fresh.
        Returns True if the backend was killed. Called after cancel
        notifications when the last stub disconnects and the backend may
        still be executing cancelled work (race window before backend
        processes the cancel notification).
        """
        if self.refcount > 0:
            # Co-tenants still attached — quarantine instead of killing
            self.quarantined = True
            logger.info(
                "backend pid=%s quarantined: has %d remaining co-tenants, "
                "will recycle when drained",
                self.pid, self.refcount,
            )
            return False
        # No consumers left — hard kill the backend process
        if self.is_alive:
            pid = self.process.pid
            # Tree-scoped kill via platform_compat, not os.killpg/os.getpgid:
            # those names do not exist on Windows, and the old `except
            # (ProcessLookupError, OSError)` here did NOT catch the resulting
            # AttributeError, so this raised out of recycle_if_idle instead of
            # degrading. Windows also ignores spawn's start_new_session=True
            # (it is `unused_start_new_session` in CPython's Windows
            # _execute_child), so there is no process group there to signal at
            # all — kill_process_tree covers both (killpg / taskkill /T) and
            # already enforces the pid <= 1, pgid <= 1 and own-process-group
            # refusals this call site would otherwise hand-roll.
            # The _async variant is mandatory from a coroutine: the Windows branch
            # spawns taskkill with a 5s timeout, which would stall the daemon's
            # loop. On POSIX it dispatches inline to the sync helper, so
            # os.killpg/os.getpgid monkeypatching still intercepts.
            recycled = True
            try:
                await platform_compat.kill_process_tree_async(pid, platform_compat.SIGKILL)
            except ValueError:
                # Refused pid (non-int, or <= 1 which is a killpg broadcast).
                recycled = False
            except (ProcessLookupError, PermissionError, OSError):
                # Tree already gone or not signalable — fall back to a
                # pid-scoped kill, as this call site did before.
                with contextlib.suppress(
                    ProcessLookupError, PermissionError, OSError, ValueError
                ):
                    await platform_compat.kill_pid_async(pid, platform_compat.SIGKILL)
            if recycled:
                self._dead_reason = "recycled after last stub detached with in-flight work"
                logger.info(
                    "backend pid=%s recycled (killed): last stub detached with "
                    "in-flight work",
                    pid,
                )
                # SEL audit: SIGKILLing a pooled backend is a security-relevant
                # action — record it in the HMAC-chained event log regardless
                # of which path (abort frame or plain disconnect) got us here.
                try:
                    SecurityEventLog().log_api_access(
                        caller="gatewayd",
                        operation="mcp-gateway.backend-recycle-kill",
                        outcome="killed",
                        source="gateway",
                        resources=f"pid={pid} server={self.pool_key.server_name}",
                        error=self._dead_reason,
                    )
                except Exception:  # pragma: no cover — audit must never break recycle
                    logger.debug("SEL audit for backend recycle kill failed", exc_info=True)
                return True
        return False

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Close stdin, wait for the process to exit, escalate to SIGKILL
        after ``timeout`` seconds. Idempotent and safe to call from
        multiple call-sites concurrently (``_shutdown_lock``).
        """
        async with self._shutdown_lock:
            if self.process.returncode is not None:
                await self._cancel_background_tasks()
                return
            try:
                self.stdin.close()
            except Exception:  # pragma: no cover — stdin may already be closed
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "backend pid=%s did not exit within %.1fs after stdin close; "
                    "escalating to SIGKILL",
                    self.process.pid, timeout,
                )
                # Kill the whole process TREE, not just the launcher PID:
                # on POSIX spawn uses start_new_session=True, so the backend is
                # a session/group leader with worker children, and
                # process.kill() SIGKILLs only the launcher, reparenting its
                # workers to init where they leak under LRU-eviction churn.
                # Via platform_compat rather than os.killpg(os.getpgid(...)):
                # neither name exists on Windows, and the except clause below
                # does not catch AttributeError — so on Windows this raised out
                # of shutdown() and the process.kill() fallback never ran,
                # leaving the backend alive. The _async variant is required per
                # test_kill_process_awaits_async_variant_not_sync (Windows
                # taskkill would otherwise block this loop).
                try:
                    await platform_compat.kill_process_tree_async(
                        self.process.pid, platform_compat.SIGKILL
                    )
                except (ProcessLookupError, PermissionError, OSError, ValueError):
                    try:
                        self.process.kill()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.error(
                        "backend pid=%s survived SIGKILL (uninterruptible sleep?)",
                        self.process.pid,
                    )
            await self._cancel_background_tasks()
            if self._dead_reason is None:
                self._dead_reason = f"shutdown rc={self.process.returncode}"


# --- Spawn / handshake ------------------------------------------------------


async def spawn_backend(
    pool_key: "PoolKey",
    command: str,
    args: list[str],
    env: Mapping[str, str],
    work_dir: str,
) -> Backend:
    """Spawn a real MCP subprocess and wrap it in a :class:`Backend`.

    ``env`` is passed verbatim — callers MUST NOT rely on parent process
    env inheritance. The rewriter layer computes the effective env for
    each :class:`PoolKey` and includes it in the hash; spawning with a
    different env than the key claims is a correctness bug that would
    allow cross-tenant leakage.

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
    logger.info(
        "spawning backend pool=%s command=%s args=%s",
        pool_key.human_readable(), command, redact(" ".join(args)),
    )
    # Positive-identity marker for the orphan sweep. Safe re: the pooled-backend
    # PoolKey invariant — the marker is a compile-time constant, so it is
    # identical for every key and cannot split or collapse pooled-backend
    # identity (unlike a per-session value, which would be a correctness bug).
    spawn_env = dict(env)
    spawn_env[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE
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
    )
    backend._last_ping_response_mono = now  # cold-start: not insta-stale
    backend._stderr_task = stderr_task
    return backend


async def send_initialize(
    backend: Backend,
    *,
    client_info: Optional[Mapping[str, Any]] = None,
    timeout: float = _DEFAULT_INITIALIZE_TIMEOUT_SECS,
) -> dict[str, Any]:
    """Send the MCP ``initialize`` request and parse the response.

    Side effect: sets ``backend.supports_caller_identity`` based on
    ``capabilities.experimental.kirocrew.caller-identity`` in the response.
    Backends that don't advertise the capability are tagged as
    caller-identity-unaware; the routing layer falls back to per-session
    spawn for them (no cross-tenant injection of ``_meta.kirocrew.caller``).

    Raises :class:`asyncio.TimeoutError` if the backend doesn't respond
    within ``timeout`` seconds, :class:`ValueError` on malformed responses.
    """
    # Invariant: this helper reads ``backend.stdout`` directly to consume the
    # initialize reply, so it MUST run before the stdout pump owns the stream.
    # Every caller today invokes it pre-pump; if a future caller runs it while
    # the pump is active the two would steal frames from each other. Fail loud
    # rather than race silently.
    if backend._stdout_task is not None:
        raise RuntimeError(
            "send_initialize() must run before the stdout pump starts; "
            "the running pump owns backend.stdout"
        )
    request = {
        "jsonrpc": "2.0",
        "id": _GATEWAY_INIT_ID,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": dict(client_info or {"name": "kirocrew-gateway", "version": "0"}),
        },
    }
    await _write_json_line(backend.stdin, request)

    # Backends may emit log lines to stdout before the JSON-RPC response;
    # skip anything that isn't a well-formed JSON-RPC object addressed to
    # our init id. Bounded by ``timeout`` so a flood of noise still fails.
    async def _await_response() -> dict[str, Any]:
        while True:
            line = await backend.stdout.readuntil(b"\n")
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.debug("backend pre-init line not JSON; dropping: %r", line[:200])
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") != _GATEWAY_INIT_ID:
                continue
            return msg

    try:
        response = await asyncio.wait_for(_await_response(), timeout=timeout)
    except asyncio.IncompleteReadError as exc:
        raise ValueError(
            f"backend closed stdout before initialize response: got {len(exc.partial)} bytes"
        ) from exc

    if "error" in response:
        raise ValueError(f"backend returned initialize error: {response['error']}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"backend initialize response missing/non-dict result: {response!r}")

    capabilities = result.get("capabilities") or {}
    experimental = capabilities.get("experimental") or {}
    backend.supports_caller_identity = isinstance(experimental, dict) and (
        CALLER_CAPABILITY_KEY in experimental
    )
    # Seed the init cache so a multi-stub flow can replay the result to
    # later attachers without re-issuing the handshake. Single-stub callers
    # (the M1 path) never observe this cache but the tests that drive the
    # full M2 flow rely on ``_init_state == "ready"`` after initialize.
    #
    # NOTE: unlike the lazy _on_upstream_initialize path, this does NOT send
    # the synthetic notifications/initialized to the backend. Correct for
    # today's callers (production spawns take the lazy path; send_initialize
    # callers don't gate on it), but a future caller that relies on the
    # backend having received notifications/initialized here would hang —
    # send it explicitly if you add such a path.
    backend._init_result = result
    backend._init_state = "ready"
    logger.info(
        "backend pid=%s initialized; supports_caller_identity=%s",
        backend.pid, backend.supports_caller_identity,
    )
    return result


# --- Helpers ----------------------------------------------------------------


async def _write_json_line(writer: asyncio.StreamWriter, obj: Any) -> None:
    """Serialize ``obj`` as one JSON-RPC line and drain the writer.

    Backpressure matters: without ``drain()`` a slow backend can let the
    OS pipe buffer fill and silently stall the gateway loop (Phase-0
    item #2). Every write goes through this helper.
    """
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"
    lock = getattr(writer, "_mc_write_lock", None)
    guard: Any = lock if lock is not None else contextlib.nullcontext()
    async with guard:
        writer.write(payload)
        # Bounded: a backend that stopped reading its stdin must not hang the
        # forwarding coroutine (and the heartbeat sweeper) forever. On timeout
        # raise a pipe error so the caller recycles the wedged backend (callers
        # treat BrokenPipeError/ConnectionResetError as BackendGone).
        try:
            await asyncio.wait_for(writer.drain(), timeout=_WRITE_DRAIN_TIMEOUT_SECS)
        except asyncio.TimeoutError as exc:
            raise BrokenPipeError("backend stdin drain timed out") from exc


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
