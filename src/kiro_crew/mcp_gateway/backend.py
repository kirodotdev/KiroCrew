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
from pathlib import Path
from typing import Any, Mapping, Optional

from kiro_crew import platform_compat
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
from kiro_crew.executors import image_executor, maintenance_executor
from kiro_crew.mcp_caller import (
    CALLER_CAPABILITY_KEY,
    CALLER_META_KEY,
    CallerContext,
    build_caller_meta,
)
from kiro_crew.mcp_gateway import hazards
from kiro_crew.mcp_gateway.apps import (
    WithheldTools,
    append_marker,
    extract_declared_ui_uris,
    extract_ui_resource_uri,
    strip_model_hidden_tools,
    write_spool,
)
from kiro_crew.mcp_gateway.backend_tmp import (
    allocate_backend_tmp,
    record_owner,
    sweep_backend_tmp,
    tmp_env,
)
from kiro_crew.mcp_gateway.image_budget import (
    line_may_carry_image_block,
    parse_image_bearing_frame,
    rewrite_image_frame,
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

# Upper bound on the number of distinct URIs one stub may hold resource
# subscriptions for. Same guard class as ``_STUB_INBOX_MAXSIZE``: table
# entries are created by stub input and freed only on unsubscribe, a server
# refusal, or detach, so without a bound one misbehaving tenant could grow
# the shared broker's table (and gateway RSS) without limit. Generous — a
# real client subscribes to a handful of URIs. Past the cap a subscribe is
# answered locally with a JSON-RPC error instead of being forwarded.
_RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB = 1024

# Upper bound on the orphaned-lease set (URIs whose gateway-originated
# release the server refused with nobody left to route to). Same guard
# class: entries accumulate for the backend's lifetime across detached
# stubs, so a server that keeps refusing releases would otherwise grow the
# set without bound. The set is telemetry hygiene (hazard suppression),
# not correctness — evicting an arbitrary entry risks at most one unfair
# hazard row, while an unbounded set is unbounded gateway memory.
_ORPHANED_LEASES_MAX = 1024

# Upper bound on a subscribable resource URI's LENGTH. The per-stub cap
# bounds how MANY subscriptions a stub holds, not their bytes: without a
# length bound, repeated accepted subscriptions to distinct near-frame-limit
# URIs retain gigabytes of dictionary keys. Real resource URIs are short;
# 8 KiB is far beyond any legitimate identifier. Overlong URIs are refused
# locally before any table stores them.
_RESOURCE_URI_MAX_LEN = 8192

# JSON-RPC error code for a gateway-refused request (server-defined range).
_JSONRPC_SERVER_ERROR = -32000

# Server->client notifications that reflect backend-wide state identical for
# every co-pooled tenant, so they are safe to fan out when we cannot attribute
# them to a single owning stub. Everything else (progress, logging/message,
# cancelled) is request-scoped: broadcasting an unattributable one would leak
# one tenant's content to co-tenants — a disclosure the non-pooled baseline
# never had — so those are dropped instead. ``notifications/resources/updated``
# is subscription-scoped, not request-scoped, and is routed by the
# ``uri -> {stub_uuid}`` table instead (``Backend._resource_update_targets``).
_GLOBAL_BROADCAST_NOTIFICATIONS: frozenset[str] = frozenset({
    "notifications/tools/list_changed",
    "notifications/prompts/list_changed",
    "notifications/resources/list_changed",
})

# MCP resource-subscription wire methods and the notification they scope.
# ``notifications/resources/updated`` carries only a URI — no request id — so
# request-scoped attribution can never route it. The broker instead records
# which stub sent ``resources/subscribe`` for which URI and delivers each
# update to exactly those stubs (see ``Backend._resource_subscriptions``).
_RESOURCES_SUBSCRIBE_METHOD = "resources/subscribe"
_RESOURCES_UNSUBSCRIBE_METHOD = "resources/unsubscribe"
_RESOURCES_UPDATED_NOTIFICATION = "notifications/resources/updated"


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
    # ``params.uri`` of a forwarded ``resources/subscribe`` /
    # ``resources/unsubscribe``, captured so the response arm can apply the
    # lease transition the reply confirms or refuses. Empty for every other
    # method.
    resource_uri: str = ""
    # For a ``resources/subscribe``/``unsubscribe`` on an identity-capable
    # server: the caller whose block was injected into the forward. Recorded
    # into ``_grant_callers`` when the server GRANTS, so a later release can
    # be sent as the principal that actually holds the grant — releasing as
    # the connection (or as whatever caller a claim rekey later mapped the
    # stub to) would be adjudicated as a different principal and could
    # revoke a co-tenant's live subscription.
    caller: Optional["CallerContext"] = None
    # Set only on a gateway-originated post-respawn subscribe replay
    # (stub_uuid == ``_RELEASE_STUB_SENTINEL``): the stub whose routing grant
    # the replayed subscribe re-establishes on success.
    replay_stub: str = ""
    # The stub that ORIGINATED a subscribe later converted to the lease
    # sentinel (retract, eviction, detach). Sentinelizing removes the
    # pending from ``stub_uuid``-keyed accounting, and without this the
    # per-stub subscription cap loses sight of it — a stub cycling
    # subscribe/unsubscribe against an unresponsive server would grow the
    # pending table without bound. Cap accounting only; never routed to.
    origin_stub: str = ""


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

# Sentinel ``stub_uuid`` for gateway-originated lease maintenance on the
# resource-subscription table: the ``resources/unsubscribe`` issued when the
# last subscriber of a URI detaches without unsubscribing (so the server does
# not keep firing updates nobody will receive), and the ``resources/subscribe``
# replayed after a transparent respawn (so a live subscription survives the
# backend swap). Responses are consumed in :meth:`Backend._route_backend_line`.
_RELEASE_STUB_SENTINEL = "__release__"


def _is_success_response(msg: dict[str, Any]) -> bool:
    """Whether a JSON-RPC response frame is a settled SUCCESS: it must carry
    a ``result`` and no ``error``. A malformed frame with neither is not a
    grant — mutating the resource-subscription routing table on its strength
    would let a co-tenant start (or stop) receiving updates on a verdict the
    server never actually delivered, so lease transitions treat it as a
    refusal (fail closed)."""
    return "error" not in msg and "result" in msg


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

    Capability follows the STUB, not a new preference. This function only ever
    runs inside a backend, and a backend only exists because a stub reached the
    broker for a server the operator stubbed, so the opt-in has already happened
    by the time control is here. That is why there is no *forward-facing* apps
    switch any more: a preference could not grant the feature (with no stub there
    is no render or callback path to grant).

    What survives is the two ways an operator can still say **no**:

    1. ``KIROCREW_MCP_APPS`` off -> disabled. Absolute kill switch, for tests,
       the e2e harness, and an operator who wants a stubbed server's backend
       shared without its server-authored UI.
    2. A stored ``mcp_gateway.apps_enabled = false`` -> disabled, EVEN with the
       env flag on. This key is retired going forward — nothing writes it, the
       MCP Management page does not surface it, and the docs no longer teach it —
       but a released version honoured it as a trustworthy opt-out, so a config
       that already carries ``false`` keeps its opt-out. Dropping it here would
       silently start executing server-authored UI for the one operator who took
       the trouble to turn it off. It defaults True when absent, so this fires
       only on a value someone actually wrote: "not configured" is not an opt-out.

    The released gate had a third leg — it also required ``mcp_gateway.enabled``,
    because back then the broker existed only when sharing was on. That leg is
    deliberately gone, and it costs no released behaviour: under the current
    migration ``enabled: false`` resolves to an EMPTY stub set, so no stub, no
    backend, and this gate never runs for such an install.

    Fails CLOSED: if config cannot be read, the feature is disabled. An
    unreadable config in gatewayd is an abnormal state, and silently disabling an
    optional rendering feature is the low-harm outcome versus rendering against
    an operator preference we could not confirm.

    Read per-call (``KiroCrewConfig.load`` is fingerprint-cached, so this is a
    dict lookup in the common case) so the gateway reflects a config change
    without a daemon restart — including a daemon this gateway merely adopted and
    therefore cannot restart.
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
    return bool(gw.apps_enabled)


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

    async def evict_stub_subscriptions(self, stub_uuid: str) -> int:
        """Evict ``stub_uuid``'s resource subscriptions because its stub is
        being REKEYED to a different caller (warm-pool ``claim``) — the stub
        stays attached, but grants belong to the OLD principal: without
        eviction the new session would keep receiving the old session's
        resource-update URIs (which can carry tokens or presigned params).

        Ownership of caller-binding lives at the gatewayd connection layer;
        this is the backend-side clearance it invokes at the moment of the
        rekey. Identity-capable grants are released AS the grant-time caller
        (shared-caller guard applies: the last sharer's eviction releases);
        identity-less routing drops the stub with a last-subscriber release.
        Returns the number of URIs evicted. In-flight and parked subscribe
        transitions are RETRACTED (each id answered with a cancellation
        error, pendings scoped so a late grant releases instead of routing):
        a subscribe sent under the old owner must not land its grant on the
        rekeyed stub. In-flight unsubscribes are SENTINELIZED (mirror of
        ``detach_stub``): their table effects are already applied, but the
        response would otherwise deliver under the OLD owner's request id
        into the rekeyed stub's stream — an id the new owner may have
        already reused for its own request.
        """
        # Bump FIRST, before any table work and any await: an in-flight
        # replay loop checks this between its writes, and the bump must be
        # visible from the moment the rekey is decided.
        self._rekey_generation[stub_uuid] = (
            self._rekey_generation.get(stub_uuid, 0) + 1
        )
        # --- retract in-flight/parked subscribe transitions (commit all
        # table changes first, then reply; replies can re-enter via a
        # full-inbox detach) ---
        rekey_error = {
            "code": _JSONRPC_SERVER_ERROR,
            "message": "resources/subscribe retracted: stub ownership changed",
        }
        retract_replies: list[Any] = []
        for table in (
            self._lease_pending_riders,
            self._lease_replacement_subscribes,
            self._lease_release_waiters,
        ):
            for uri in list(table):
                mine = [r for r in table[uri] if r[0] == stub_uuid]
                if not mine:
                    continue
                remaining = [r for r in table[uri] if r[0] != stub_uuid]
                if remaining:
                    table[uri] = remaining
                else:
                    del table[uri]
                retract_replies.extend(rid for _u, rid in mine if rid is not None)
        for p in self._pending_requests.values():
            if (
                p.stub_uuid == stub_uuid
                and p.method == _RESOURCES_UNSUBSCRIBE_METHOD
                and p.resource_uri
            ):
                # The release was sent under the OLD owner: its response
                # would otherwise deliver under the old request id into the
                # rekeyed stub's stream — an id the new owner may have
                # already reused. Sentinelize (mirror of ``detach_stub``):
                # the response settles silently, table effects stay applied.
                p.origin_stub = stub_uuid
                p.stub_uuid = _RELEASE_STUB_SENTINEL
                p.original_id = None
                continue
            if (
                p.stub_uuid == stub_uuid
                and p.method == _RESOURCES_SUBSCRIBE_METHOD
                and p.resource_uri
            ):
                if p.original_id is not None:
                    retract_replies.append(p.original_id)
                riders = (
                    []
                    if self.supports_caller_identity
                    else self._lease_pending_riders.get(p.resource_uri, [])
                )
                if riders:
                    p.stub_uuid, p.original_id = riders.pop(0)
                else:
                    # Sentinelize keeping ``caller``: the late grant then
                    # finds no replay stub and releases the lease AS the
                    # old principal instead of routing it to the new owner.
                    p.origin_stub = stub_uuid
                    p.stub_uuid = _RELEASE_STUB_SENTINEL
                    p.original_id = None
                continue
            if (
                p.stub_uuid == _RELEASE_STUB_SENTINEL
                and p.method == _RESOURCES_SUBSCRIBE_METHOD
                and p.replay_stub == stub_uuid
            ):
                # An in-flight REPLAY targets the rekeyed stub by its
                # ``replay_stub`` back-reference, not by ``stub_uuid`` — the
                # subscribe retraction above cannot see it.  Left attached,
                # its late grant would commit routing (and, on an
                # identity-capable server, a grant-caller record) for a stub
                # the OLD owner subscribed but the NEW owner now holds.
                # Scope it to release-on-grant, exactly as the replay
                # write-failure path does: nobody consumes the grant, so it
                # releases instead of routing.
                p.replay_stub = ""
        # --- commit ALL routing-table removals before the FIRST await:
        # the claim path reassigns the owner then awaits this eviction, so
        # any frame routed during an await must already see the stub gone
        # from the tables ---
        evicted = 0
        releases: list[tuple[str, Optional[CallerContext]]] = []
        if self.supports_caller_identity:
            # Walk the ROUTING TABLE, not ``_grant_callers``: a routed grant
            # can lack a caller record (the grant arm records one only when
            # the accepted subscribe carried caller metadata), and keying
            # the eviction on the records alone would leave that entry
            # routed — the new owner would keep receiving the old owner's
            # URIs. Mirror of ``detach_stub``: known-caller grants release
            # as their principal; unknown-caller grants are knowingly
            # RETAINED and orphan-marked (a bare release would be
            # adjudicated as the wrong principal).
            id_emptied: list[str] = []
            for uri, subscribers in self._resource_subscriptions.items():
                if stub_uuid in subscribers:
                    subscribers.discard(stub_uuid)
                    evicted += 1
                    if not subscribers:
                        id_emptied.append(uri)
            departed_keys = [
                key for key in self._grant_callers if key[1] == stub_uuid
            ]
            for uri, _stub in departed_keys:
                grant_caller = self._grant_callers.pop((uri, stub_uuid))
                shared = any(
                    other != stub_uuid
                    and self._grant_callers.get((uri, other)) == grant_caller
                    for other in self._resource_subscriptions.get(uri, set())
                )
                if not shared:
                    releases.append((uri, grant_caller))
            for uri in id_emptied:
                del self._resource_subscriptions[uri]
                if not any(u == uri for u, _c in releases):
                    while len(self._orphaned_leases) >= _ORPHANED_LEASES_MAX:
                        self._orphaned_leases.pop()
                    self._orphaned_leases.add(uri)
        else:
            emptied: list[str] = []
            for uri, subscribers in list(self._resource_subscriptions.items()):
                if stub_uuid in subscribers:
                    subscribers.discard(stub_uuid)
                    evicted += 1
                    if not subscribers:
                        emptied.append(uri)
            for uri in emptied:
                del self._resource_subscriptions[uri]
                if uri not in self._lease_awaiting_release:
                    releases.append((uri, None))
        # --- state committed; now the awaits ---
        for reply_id in retract_replies:
            await self._reply_locally(stub_uuid, reply_id, error=rekey_error)
        for uri, release_caller in releases:
            await self._release_upstream_subscriptions(
                [uri], caller=release_caller)
        return evicted

    async def detach_stub(self, stub_uuid: str) -> int:
        """Drop ``stub_uuid``'s inbox and clean up any pending requests
        owned by it. Returns the remaining refcount so the caller can
        decide whether to trigger a drain.
        """
        async with self._inbox_lock:
            self._stub_inboxes.pop(stub_uuid, None)
            self.refcount = len(self._stub_inboxes)
        # Bounded-table hygiene. (A replay in flight for a detached stub is
        # already handled at grant time: a replay grant is honoured only
        # while its stub is still attached.)
        self._rekey_generation.pop(stub_uuid, None)
        # A departing stub parked as a rider expects no further reply. This
        # prune runs BEFORE the pending scan below: the scan promotes the
        # first parked rider into the departing stub's in-flight subscribe,
        # and a duplicate parking owned by the departing stub itself would
        # otherwise be promoted — recording a phantom subscriber that blocks
        # the release and swallows updates.
        for uri in list(self._lease_pending_riders):
            remaining = [
                r for r in self._lease_pending_riders[uri] if r[0] != stub_uuid
            ]
            if remaining:
                self._lease_pending_riders[uri] = remaining
            else:
                del self._lease_pending_riders[uri]
        # Likewise a departing stub parked behind an in-flight release.
        for uri in list(self._lease_release_waiters):
            remaining = [
                r for r in self._lease_release_waiters[uri] if r[0] != stub_uuid
            ]
            if remaining:
                self._lease_release_waiters[uri] = remaining
            else:
                del self._lease_release_waiters[uri]
        # And a departing stub whose replacement subscribe is parked.
        for uri in list(self._lease_replacement_subscribes):
            remaining = [
                r for r in self._lease_replacement_subscribes[uri]
                if r[0] != stub_uuid
            ]
            if remaining:
                self._lease_replacement_subscribes[uri] = remaining
            else:
                del self._lease_replacement_subscribes[uri]
        # Drop any pending-request entries owned by the departing stub so the
        # stdout pump does not try to send into a dead inbox — EXCEPT an
        # in-flight coalesced lease transition, whose response still has to
        # settle shared state: a subscribe with parked riders is handed to the
        # first rider (which then receives the server's verdict under its own
        # id); one without riders is converted to the lease sentinel so a
        # granted-but-unwanted lease is released instead of leaking.
        stale = [fid for fid, p in self._pending_requests.items() if p.stub_uuid == stub_uuid]
        for fid in stale:
            p = self._pending_requests.get(fid)
            if p is None:
                continue
            if p.resource_uri and p.method == _RESOURCES_SUBSCRIBE_METHOD:
                # BOTH regimes: dropping the pending would drop the server's
                # verdict — a late identity grant would strand the upstream
                # lease with nobody to release it. Coalesced: hand it to the
                # first parked rider; otherwise (including every identity
                # pending, which never has riders) convert to the sentinel,
                # whose grant arm releases a granted-but-unwanted lease as
                # ``pending.caller`` — the right principal.
                riders = (
                    []
                    if self.supports_caller_identity
                    else self._lease_pending_riders.get(p.resource_uri, [])
                )
                if riders:
                    p.stub_uuid, p.original_id = riders.pop(0)
                else:
                    p.origin_stub = stub_uuid
                    p.stub_uuid = _RELEASE_STUB_SENTINEL
                    p.original_id = None
                continue
            if p.resource_uri and p.method == _RESOURCES_UNSUBSCRIBE_METHOD:
                # Let the release-in-flight settle silently; the table entry
                # is pruned below either way.
                p.origin_stub = stub_uuid
                p.stub_uuid = _RELEASE_STUB_SENTINEL
                p.original_id = None
                continue
            self._pending_requests.pop(fid, None)
        # Drop the departing stub from the resource-subscription table so a
        # later ``notifications/resources/updated`` is not routed toward a
        # departed stub — and release upstream whatever the departure
        # strands. Identity-less: release when the departing stub was a
        # URI's LAST subscriber (the one shared lease). Identity-capable:
        # every grant is per caller, so the departing stub's grant is
        # released AS the caller recorded at grant time — unless another
        # still-routed stub shares that same caller for the URI (two stubs
        # claimed to one session hold ONE per-caller grant upstream;
        # releasing on the first detach would kill the survivor's updates).
        # Without a release the server keeps firing updates nobody will
        # receive, each landing in the deny-by-default drop and unfairly
        # recording a hazard against the server for a gap that is ours.
        # ALL table transitions commit before the release writes (a write
        # can re-enter detach via a full-inbox drop).
        emptied = []
        for uri, subscribers in self._resource_subscriptions.items():
            subscribers.discard(stub_uuid)
            if not subscribers:
                emptied.append(uri)
        if self.supports_caller_identity:
            identity_releases: list[tuple[str, CallerContext]] = []
            departed_keys = [
                key for key in self._grant_callers if key[1] == stub_uuid
            ]
            for uri, _stub in departed_keys:
                grant_caller = self._grant_callers.pop((uri, stub_uuid))
                shared = any(
                    other != stub_uuid
                    and self._grant_callers.get((uri, other)) == grant_caller
                    for other in self._resource_subscriptions.get(uri, set())
                )
                if not shared:
                    identity_releases.append((uri, grant_caller))
            for uri in emptied:
                del self._resource_subscriptions[uri]
                if not any(u == uri for u, _c in identity_releases):
                    # Routed grant with no recorded caller (identity server,
                    # grant predates tracking or replay went unrecorded): a
                    # bare release would be adjudicated as the wrong
                    # principal, so the lease is knowingly RETAINED — mark
                    # it orphaned so its updates are dropped without
                    # charging a hazard to a blameless server.
                    while len(self._orphaned_leases) >= _ORPHANED_LEASES_MAX:
                        self._orphaned_leases.pop()
                    self._orphaned_leases.add(uri)
            for uri, grant_caller in identity_releases:
                await self._release_upstream_subscriptions(
                    [uri], caller=grant_caller)
        else:
            to_release = []
            for uri in emptied:
                del self._resource_subscriptions[uri]
                # A URI whose release is already in flight (final
                # unsubscribe forwarded, response pending) is not released
                # twice; its awaiting-release flag stays up so the
                # converted-to-sentinel response settles it (marking the
                # lease orphaned on a refusal).
                if uri not in self._lease_awaiting_release:
                    to_release.append(uri)
            if to_release:
                await self._release_upstream_subscriptions(to_release)
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

        if method in (_RESOURCES_SUBSCRIBE_METHOD, _RESOURCES_UNSUBSCRIBE_METHOD):
            # Subscription lease bookkeeping. Returns True when the request
            # was answered locally (a later subscriber joining a held lease,
            # or a non-final unsubscribe) and must NOT reach the backend —
            # the server holds one subscription per URI, and forwarding
            # either frame would break a co-tenant's live subscription.
            if await self._handle_resource_subscription(stub_uuid, method, msg):
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
                resource_uri = ""
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
                    if method in (_RESOURCES_SUBSCRIBE_METHOD, _RESOURCES_UNSUBSCRIBE_METHOD):
                        _uri = _params.get("uri")
                        if isinstance(_uri, str):
                            resource_uri = _uri
                self._pending_requests[fid] = _PendingRequest(
                    stub_uuid=stub_uuid, original_id=orig_id, method=str(method or ""),
                    t_start_ms=time.monotonic() * 1000.0,
                    progress_token=progress_token,
                    session_key=(caller.session_key if caller is not None else ""),
                    tool_name=tool_name,
                    tool_arguments=tool_arguments,
                    resource_uri=resource_uri,
                    caller=(caller if resource_uri else None),
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
        # Armed only once the forward is on the wire: a write that failed never
        # started a handshake, and arming there would leave a timer with no
        # in-flight window to close.
        self._arm_init_deadline()

    def _arm_init_deadline(self) -> None:
        """Start the timer that fails and reaps a backend which never answers
        the first ``initialize``.

        Idempotent per in-flight window: an already-armed timer is left alone
        so queued stubs cannot each extend the deadline.
        """
        if self._init_deadline_task is not None and not self._init_deadline_task.done():
            return
        self._init_deadline_task = asyncio.create_task(
            self._init_deadline(_DEFAULT_INITIALIZE_TIMEOUT_SECS)
        )

    def _cancel_init_deadline(self) -> None:
        """Disarm the first-handshake timer once init reaches a terminal state.

        Safe from inside the timer's own coroutine: cancelling the currently
        running task is skipped, so a terminal transition driven BY the
        deadline does not cancel itself mid-flight.
        """
        task = self._init_deadline_task
        if task is None:
            return
        self._init_deadline_task = None
        if task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()

    async def _init_deadline(self, timeout: float) -> None:
        """Fail and reap a backend whose first ``initialize`` never resolves.

        ``_fail_init`` performs the terminal transition every waiter observes
        (failed state, done event, an explicit JSON-RPC error to each queued
        stub); ``shutdown`` then reaps the process group, since a wedged
        backend that is still running would otherwise hold its core until the
        idle sweep reclaims it. shutdown() touches no pool bookkeeping, so
        there is no reserve/evict race with a concurrent acquirer.
        """
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        # The handshake may have resolved while this coroutine was scheduled;
        # only a still-in-flight window is this timer's to close.
        if self._init_state != "in_flight":
            return
        reason = f"initialize did not complete within {timeout:g}s"
        self._dead_reason = self._dead_reason or reason
        with contextlib.suppress(Exception):
            await self._fail_init(reason)
        with contextlib.suppress(Exception):
            await self.shutdown()

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
        timeout: float = _DEFAULT_INITIALIZE_TIMEOUT_SECS,
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
        # The backend is terminal from here, so the first-handshake timer has
        # nothing left to close. Disarming is a no-op when the deadline itself
        # drove this broadcast.
        self._cancel_init_deadline()
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
        # Preserve replay-target URIs BEFORE the clear: an in-flight replay
        # is the only record of its URI (routing commits on grant), and this
        # backend dying is exactly the case where the grant never arrives.
        # The next respawn's ``resource_subscription_uris`` capture reads
        # these; a rekey-evicted replay has ``replay_stub == ""`` and is
        # correctly NOT preserved (the old owner's URI must not follow the
        # rekeyed stub).
        for pending in self._pending_requests.values():
            if (
                pending.replay_stub
                and pending.method == _RESOURCES_SUBSCRIBE_METHOD
                and pending.resource_uri
            ):
                self._gone_replay_uris.setdefault(
                    pending.replay_stub, set()
                ).add(pending.resource_uri)
        self._pending_requests.clear()
        self._init_pending.clear()
        # Parked lease work is NOT in ``_pending_requests``: a rider waiting
        # on an in-flight grant, a replacement subscribe parked behind a
        # release, and an unsubscribe waiting on a release verdict each hold
        # a client request id that only a backend response would settle —
        # and no response is coming. Answer each with the same synthetic
        # error so no client id hangs forever (a replay parking carries no
        # id and drops silently), then clear the lease coordination state:
        # it gates writes onto a wire that no longer exists. The routing
        # table ``_resource_subscriptions`` is deliberately KEPT — the
        # transparent respawn reads it to replay live subscriptions onto the
        # replacement backend. A full-inbox detach during these replies can
        # re-enter teardown paths; the tables were already cleared above the
        # replies, so re-entry finds them empty.
        parked: list[tuple[str, Any]] = []
        for table in (
            self._lease_pending_riders,
            self._lease_replacement_subscribes,
            self._lease_release_waiters,
        ):
            for entries in table.values():
                parked.extend(entries)
            table.clear()
        self._lease_awaiting_grant.clear()
        self._lease_awaiting_release.clear()
        for parked_uuid, parked_id in parked:
            if parked_id is None:
                continue
            inbox = inboxes.get(parked_uuid)
            if inbox is None:
                continue
            err = {
                "jsonrpc": "2.0",
                "id": parked_id,
                "error": {"code": -32000, "message": f"backend gone: {reason}"},
            }
            await self._enqueue_to_stub(
                parked_uuid, inbox,
                (json.dumps(err, separators=(",", ":")) + "\n").encode("utf-8"),
            )

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
                    # ``READ_BUFFER_LIMIT_BYTES`` (64 MiB by default, and
                    # operator-tunable); a longer line is pathological and dropped.
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
                # Enforce the inline-image budget on tool results BEFORE the
                # spill step: a downscaled image both shrinks what spill writes
                # to disk and, more importantly, keeps an oversized image block
                # out of kiro-cli's conversation history, where it would be
                # replayed to the model on every later turn and wedge the
                # session (see kiro_crew.imaging MAX_IMAGE_EDGE_PX). Two
                # stages on two pools: the byte probe admits every frame that
                # COULD carry an image block (its negative is provable, but
                # any escaped non-ASCII text also matches), so a cheap
                # parse-confirm runs on the maintenance pool first -- like the
                # spill rewrite -- and only genuinely image-bearing frames
                # reach the image pool, where seconds-long Pillow decodes
                # from one server would otherwise head-of-line block every
                # other server's text-only results behind the probe's false
                # positives.
                if line_may_carry_image_block(line):
                    try:
                        loop = asyncio.get_running_loop()
                        image_msg = await loop.run_in_executor(
                            maintenance_executor(),
                            parse_image_bearing_frame,
                            line,
                        )
                        if image_msg is not None:
                            line = await loop.run_in_executor(
                                image_executor(),
                                rewrite_image_frame,
                                image_msg,
                                line,
                                self.pool_key.server_name,
                            )
                    except Exception:
                        # The rewrite never RAN (executor shutdown/saturation);
                        # per-block fail-closed lives inside the hook. Routing
                        # the raw line keeps co-pooled tenants alive, but the
                        # frame may carry an unverified image -- log loudly
                        # enough to diagnose a wedge that follows.
                        logger.warning(
                            "image-budget rewrite could not run for %s; routing raw line",
                            self.pool_key.server_name,
                            exc_info=True,
                        )
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
            if pending.stub_uuid == _RELEASE_STUB_SENTINEL:
                # Gateway-originated lease maintenance (release after the last
                # subscriber detached, a post-respawn replay, or a transition
                # orphaned by its forwarder retracting/detaching mid-flight).
                # Riders that parked on the URI after the orphaning are
                # settled on the server's verdict exactly as the normal grant
                # arm settles them — dropping a parking here would leave that
                # stub's subscribe swallowed with no response, hung forever.
                if pending.method == _RESOURCES_SUBSCRIBE_METHOD and pending.resource_uri:
                    orphan_uri = pending.resource_uri
                    self._lease_awaiting_grant.discard(orphan_uri)
                    riders = self._lease_pending_riders.pop(orphan_uri, [])
                    if _is_success_response(msg):
                        granted: set[str] = set()
                        # A replay grant is honoured only while its stub is
                        # still attached — detach cannot see a sentinel-owned
                        # pending, so this is where a mid-replay disconnect is
                        # caught; granting the dead UUID would pin the lease
                        # to a stub that can never drain it.
                        if (
                            pending.replay_stub
                            and pending.replay_stub in self._stub_inboxes
                        ):
                            granted.add(pending.replay_stub)
                        granted.update(rider_uuid for rider_uuid, _ in riders)
                        # Routing is committed BEFORE any reply is awaited: a
                        # reply into a full inbox detaches its stub, which
                        # prunes the live table — updating the table from a
                        # local set afterwards would reinsert the detached
                        # UUID and route updates at a stub that is gone.
                        if granted:
                            self._resource_subscriptions.setdefault(
                                orphan_uri, set()).update(granted)
                            self._orphaned_leases.discard(orphan_uri)
                            if (
                                self.supports_caller_identity
                                and pending.caller is not None
                                and pending.replay_stub in granted
                            ):
                                # A replayed identity grant is held by the
                                # replay's caller — record it so a later
                                # detach can release as the right principal.
                                self._grant_callers[
                                    (orphan_uri, pending.replay_stub)
                                ] = pending.caller
                        elif orphan_uri not in self._resource_subscriptions:
                            # A granted lease nobody wants: release on the
                            # spot, as the caller that took it (a replay's
                            # grant belongs to the replay's principal).
                            await self._release_upstream_subscriptions(
                                [orphan_uri], caller=pending.caller)
                        for rider_uuid, rider_id in riders:
                            await self._reply_locally(rider_uuid, rider_id, result={})
                    else:
                        error_obj = msg.get("error")
                        # A refusal proves the server holds no lease; a
                        # MALFORMED frame (no error either) proves nothing —
                        # the subscribe may well have taken. Denying routing
                        # while keeping such a lease would strand it live
                        # upstream, with every later update charged to the
                        # server as a hazard, so an unsettled verdict with
                        # nobody routed releases the lease on the spot.
                        if error_obj is None and (
                            orphan_uri not in self._resource_subscriptions
                        ):
                            await self._release_upstream_subscriptions(
                                [orphan_uri], caller=pending.caller)
                        for rider_uuid, rider_id in riders:
                            await self._reply_locally(
                                rider_uuid, rider_id,
                                error=error_obj if isinstance(error_obj, dict) else {
                                    "code": _JSONRPC_SERVER_ERROR,
                                    "message": "resources/subscribe refused by server",
                                },
                            )
                elif pending.method == _RESOURCES_UNSUBSCRIBE_METHOD and pending.resource_uri:
                    # A gateway-originated (or detach-orphaned) release
                    # settled. On success the lease is cleanly gone; on a
                    # refusal with nobody left to route to, the server has
                    # RETAINED a subscription that is now a consequence of
                    # the broker's own lease handling — its updates are
                    # dropped without recording a hazard, so the server is
                    # not condemned for behaviour that is correct. A
                    # MALFORMED release response is DELIBERATELY treated as
                    # a refusal here too: on the routing axis that fails
                    # closed, and on the hazard-ledger axis it fails open
                    # (orphan-marked, so unknowable frames are forgiven
                    # rather than charged to the server).
                    _rel_uri = pending.resource_uri
                    self._lease_awaiting_release.discard(_rel_uri)
                    waiters = self._lease_release_waiters.pop(_rel_uri, [])
                    if _is_success_response(msg):
                        self._orphaned_leases.discard(_rel_uri)
                        subscribers = self._resource_subscriptions.get(_rel_uri)
                        if subscribers is not None:
                            for waiter_uuid, _waiter_id in waiters:
                                subscribers.discard(waiter_uuid)
                            if not subscribers:
                                del self._resource_subscriptions[_rel_uri]
                        for waiter_uuid, waiter_id in waiters:
                            await self._reply_locally(
                                waiter_uuid, waiter_id, result={})
                        await self._drain_replacement_subscribes(
                            _rel_uri, released=True)
                    else:
                        if _rel_uri not in self._resource_subscriptions:
                            while len(self._orphaned_leases) >= _ORPHANED_LEASES_MAX:
                                self._orphaned_leases.pop()
                            self._orphaned_leases.add(_rel_uri)
                        error_obj = msg.get("error")
                        for waiter_uuid, waiter_id in waiters:
                            await self._reply_locally(
                                waiter_uuid, waiter_id,
                                error=error_obj if isinstance(error_obj, dict) else {
                                    "code": _JSONRPC_SERVER_ERROR,
                                    "message": "resources/unsubscribe "
                                               "refused by server",
                                },
                            )
                        await self._drain_replacement_subscribes(
                            _rel_uri, released=False)
                return
            if pending.resource_uri and pending.method in (
                _RESOURCES_SUBSCRIBE_METHOD, _RESOURCES_UNSUBSCRIBE_METHOD
            ):
                await self._on_resource_subscription_response(pending, msg)
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
            # Subscription-scoped: ``notifications/resources/updated`` carries
            # no request id, so it is attributed by URI — delivered to every
            # stub subscribed to that URI and only those. This arm is
            # TERMINAL: URI is the only permitted attribution for this
            # notification, so an update for a URI nobody subscribed to is
            # dropped here (deny-by-default, hazard recorded) and never falls
            # through to request-scoped attribution — a server-supplied
            # ``_meta.relatedRequestId`` must not hand one tenant a URI that
            # only a co-tenant ever named.
            if method == _RESOURCES_UPDATED_NOTIFICATION:
                targets = self._resource_update_targets(msg)
                for target_uuid in targets:
                    await self._deliver_to_stub(target_uuid, msg)
                if not targets:
                    _params = msg.get("params")
                    _uri = _params.get("uri") if isinstance(_params, dict) else None
                    if isinstance(_uri, str) and _uri in self._orphaned_leases:
                        # A retained lease the broker failed to release: the
                        # update is a consequence of OUR lease handling, so
                        # it is dropped without condemning the server.
                        # The URI is deliberately NOT logged — resource URIs
                        # can carry tokens or presigned query parameters.
                        logger.debug(
                            "backend pid=%s dropping resources/updated for "
                            "an orphaned lease (no hazard)", self.pid,
                        )
                    else:
                        logger.debug(
                            "backend pid=%s dropping resources/updated for a "
                            "URI no stub subscribed to (never routed by "
                            "request id)", self.pid,
                        )
                        self._record_hazard(hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)
                return
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
                self._record_hazard(hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)
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
                self._record_hazard(hazards.HAZARD_UNROUTABLE_SERVER_REQUEST)
                self._dead_reason = self._dead_reason or reason
                await self._broadcast_backend_gone(reason)
            return
        logger.debug("backend pid=%s emitted malformed JSON-RPC: %r", self.pid, msg)

    def _record_hazard(self, code: str) -> None:
        """Note that this server exhibited per-client behaviour while shared.

        Only meaningful once MORE THAN ONE client is attached. A backend serving
        a single client legitimately owns it, so an unattributable frame there
        proves nothing: there is no second tenant it could have leaked to.
        ``exclusive_token`` is not sufficient to express that — a pooled backend
        also serves exactly one client from the moment it starts until a second
        stub attaches, and recording during that window would disqualify a
        server for behaviour that is correct.

        Biased toward under-recording on purpose. A false hazard withdraws a
        recommendation for a server that is fine, so the bar is real traffic on a
        genuinely shared backend; a missed one costs a withdrawal that the next
        observation makes again.

        The observation is stamped with what this backend actually launched, read
        straight off the pool key, so upgrading or reconfiguring the server
        invalidates it rather than holding the new version responsible for the
        behaviour of the one it replaced.

        In-memory only — the flush is off-loop.
        """
        if self.exclusive_token or self.refcount <= 1:
            return
        key = self.pool_key
        name = key.server_name
        identity = hazards.launch_identity(
            key.command_args_hash, key.effective_env_hash, key.binary_version
        )
        if name and hazards.record_observed(name, code, identity):
            logger.warning(
                "hazard: server %r first exhibited %s while shared; the "
                "MCP page will withdraw its recommendation",
                name, code,
            )

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
        self._cancel_init_deadline()
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
        self._cancel_init_deadline()
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

    async def _handle_resource_subscription(
        self, stub_uuid: str, method: str, msg: dict[str, Any]
    ) -> bool:
        """Apply subscription-lease bookkeeping for a stub-forwarded
        ``resources/subscribe`` / ``resources/unsubscribe``; return ``True``
        when the request was answered locally and must not be forwarded.

        Routing grants are recorded on the server's RESPONSE, never at
        forward time, so the table only ever names stubs whose subscription
        the server actually accepted (fail-closed: an update racing the
        response is dropped, not mis-routed).

        Two regimes, split on ``supports_caller_identity``:

        - An identity-capable server authorizes per caller, so EVERY
          subscribe is forwarded with its own caller block and every stub
          gets its own upstream authorization decision — no local coalescing
          on the subscribe side (``_on_resource_subscription_response``
          records the per-stub grant).
        - An identity-less server sees every co-tenant as one principal, so
          duplicate traffic tells it nothing; only lease TRANSITIONS reach
          it. The first subscriber's subscribe takes the lease; a stub
          arriving while that grant is in flight is PARKED and answered with
          the server's actual verdict, never a premature success; a stub
          arriving after the grant joins locally with the MCP empty result
          (exactly as the initialize cache answers later stubs). A non-final
          unsubscribe is answered locally; the final one is forwarded and the
          routing entry is kept until the server confirms, because a failed
          unsubscribe means the server RETAINED the subscription and dropping
          routing early would silently discard live updates.

        A request without a well-formed ``params.uri`` string is forwarded
        verbatim with no table change: the backend rejects it with its own
        error, and an entry keyed on garbage could never match an update.
        """
        params = msg.get("params")
        uri = params.get("uri") if isinstance(params, dict) else None
        if not isinstance(uri, str) or not uri:
            return False
        original_id = msg.get("id")
        if len(uri) > _RESOURCE_URI_MAX_LEN:
            # The per-stub cap bounds subscription COUNT, not bytes: 1024
            # accepted near-frame-limit URIs would retain gigabytes of
            # dictionary keys. Refuse locally before any table stores the
            # key; the URI itself is never logged.
            await self._reply_locally(
                stub_uuid, original_id,
                error={
                    "code": _JSONRPC_SERVER_ERROR,
                    "message": "resource URI exceeds the maximum supported "
                               "length",
                },
            )
            return True
        if original_id is None:
            # An id-less subscribe/unsubscribe is a notification-shaped frame
            # the MCP spec does not define. No response can ever settle the
            # lease transition it would start, so mutating lease state on it
            # would wedge the URI permanently (later subscribers park behind
            # a grant whose response never comes), and forwarding it would
            # open a server-side subscription the broker never routes.
            # Swallow it: a request without an id expects no reply.
            return True
        if method == _RESOURCES_SUBSCRIBE_METHOD:
            # Cap check precedes EVERY subscribe branch, so a capped stub can
            # neither open new leases nor keep joining co-tenants' URIs.
            if self._stub_subscription_count(stub_uuid) >= _RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB:
                # The URI is deliberately NOT logged: resource URIs can carry
                # credentials (tokens, presigned query params) and this line
                # lands in the operator's plain-text log.
                logger.warning(
                    "backend pid=%s stub=%s at resource-subscription cap (%d); "
                    "refusing subscribe",
                    self.pid, stub_uuid, _RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB,
                )
                await self._reply_locally(
                    stub_uuid, original_id,
                    error={
                        "code": _JSONRPC_SERVER_ERROR,
                        "message": "resource subscription limit reached",
                    },
                )
                return True
            if any(
                p.stub_uuid == stub_uuid
                and p.method == _RESOURCES_UNSUBSCRIBE_METHOD
                and p.resource_uri == uri
                for p in self._pending_requests.values()
            ):
                # This stub's own unsubscribe for the URI is still in flight.
                # Its response may arrive AFTER a new grant (out-of-order
                # server) and would then erase it — the releasing stub and the
                # re-subscriber are the same uuid, so the per-stub discard
                # cannot tell them apart. Refuse the resubscribe; the client
                # retries once the unsubscribe settles.
                await self._reply_locally(
                    stub_uuid, original_id,
                    error={
                        "code": _JSONRPC_SERVER_ERROR,
                        "message": "resources/unsubscribe for this URI is "
                                   "still in flight; retry after it completes",
                    },
                )
                return True
            if self.supports_caller_identity:
                # Per-caller authorization: the server must see this stub's
                # own subscribe (the caller block is injected downstream).
                return False
            if uri in self._lease_awaiting_grant:
                self._lease_pending_riders.setdefault(uri, []).append(
                    (stub_uuid, original_id))
                return True
            subscribers = self._resource_subscriptions.get(uri)
            if subscribers is not None and uri not in self._lease_awaiting_release:
                subscribers.add(stub_uuid)
                await self._reply_locally(stub_uuid, original_id, result={})
                return True
            if uri in self._lease_awaiting_release:
                # A release for this URI is in flight. The one wire orders
                # the WRITES, but the server may ANSWER out of order — a
                # replacement grant landing before the release would leave
                # local routing keeping this subscriber while the release
                # then destroys the lease upstream: silent update loss.
                # Park until the release settles; the drain either joins a
                # retained lease locally or takes a fresh one with a
                # subscribe forwarded strictly AFTER the release settled.
                self._lease_replacement_subscribes.setdefault(uri, []).append(
                    (stub_uuid, original_id))
                return True
            # First subscriber: forward to take the lease.
            self._lease_awaiting_grant.add(uri)
            return False
        # --- resources/unsubscribe ---
        if self.supports_caller_identity:
            # Retract this stub's own in-flight subscribe for the URI first
            # (mirror of the coalesced retract arm): its grant may be
            # answered AFTER this unsubscribe on an out-of-order server,
            # which would record routing for a stub that already left —
            # stranding the upstream lease. Sentinelize keeping ``caller``
            # so the late grant releases as the right principal; commit the
            # transitions, then reply.
            retracted_ids: list[Any] = []
            for p in self._pending_requests.values():
                if (
                    p.stub_uuid == stub_uuid
                    and p.method == _RESOURCES_SUBSCRIBE_METHOD
                    and p.resource_uri == uri
                ):
                    if p.original_id is not None:
                        retracted_ids.append(p.original_id)
                    p.origin_stub = stub_uuid
                    p.stub_uuid = _RELEASE_STUB_SENTINEL
                    p.original_id = None
            if retracted_ids:
                retract_error = {
                    "code": _JSONRPC_SERVER_ERROR,
                    "message": "resources/subscribe retracted by a later "
                               "unsubscribe",
                }
                for retracted_id in retracted_ids:
                    await self._reply_locally(
                        stub_uuid, retracted_id, error=retract_error)
                if stub_uuid not in self._resource_subscriptions.get(uri, set()):
                    # Nothing granted yet: the retract settles everything —
                    # the sentinelized pending releases any late grant, so
                    # the unsubscribe is truthfully answered locally.
                    await self._reply_locally(stub_uuid, original_id, result={})
                    return True
            # A stub that holds NO grant for this URI must be answered
            # locally, never forwarded: the server adjudicates an
            # unsubscribe by CALLER, so a non-holder's forwarded frame
            # would remove a same-caller HOLDER's live lease while that
            # holder stayed locally routed — updates silently stop. (The
            # retract arm above already handled the in-flight-subscribe
            # case; this covers a stub with nothing outstanding at all.)
            if stub_uuid not in self._resource_subscriptions.get(uri, set()):
                await self._reply_locally(stub_uuid, original_id, result={})
                return True
            # Two stubs claimed to one session hold ONE per-caller grant
            # upstream: forwarding either stub's unsubscribe would destroy
            # the survivor's subscription while it stays locally routed.
            # When another routed stub shares this stub's grant caller,
            # settle locally — drop only this stub — and leave the upstream
            # lease to the last sharer (same guard the detach path applies).
            grant_caller = self._grant_callers.get((uri, stub_uuid))
            if grant_caller is not None and any(
                other != stub_uuid
                and self._grant_callers.get((uri, other)) == grant_caller
                for other in self._resource_subscriptions.get(uri, set())
            ):
                self._grant_callers.pop((uri, stub_uuid), None)
                subscribers = self._resource_subscriptions.get(uri)
                if subscribers is not None:
                    subscribers.discard(stub_uuid)
                    if not subscribers:
                        del self._resource_subscriptions[uri]
                await self._reply_locally(stub_uuid, original_id, result={})
                return True
            # Sole holder (or unknown grant): forward, and mutate routing on
            # the response.
            return False
        if uri in self._lease_awaiting_grant:
            # Retract while the grant is in flight. Every subscribe id this
            # stub has outstanding for the URI is ANSWERED (with a
            # cancellation error) before its parking or pending is removed or
            # reassigned — a request silently dropped from the tables would
            # hang in the client's id table forever. A parked rider is then
            # unparked; the FORWARDER's pending is promoted to the first
            # remaining rider, or converted to the lease sentinel so a
            # granted-but-unwanted lease is released.
            retract_error = {
                "code": _JSONRPC_SERVER_ERROR,
                "message": "resources/subscribe retracted by a later unsubscribe",
            }
            riders = self._lease_pending_riders.get(uri, [])
            retracted = [r for r in riders if r[0] == stub_uuid]
            self._lease_pending_riders[uri] = [r for r in riders if r[0] != stub_uuid]
            # EVERY table transition is committed before the first reply is
            # awaited: a reply into a full inbox detaches the stub, and
            # detach both mutates the pending table mid-iteration and
            # re-decides the very promotion this arm is making — the late
            # reassignment would overwrite detach's promoted rider with the
            # sentinel, leaving that rider's subscribe unanswered forever.
            forwarder_reply_id: Any = None
            forwarder_retracted = False
            for p in self._pending_requests.values():
                if (
                    p.stub_uuid == stub_uuid
                    and p.method == _RESOURCES_SUBSCRIBE_METHOD
                    and p.resource_uri == uri
                ):
                    forwarder_retracted = True
                    forwarder_reply_id = p.original_id
                    remaining = self._lease_pending_riders.get(uri, [])
                    if remaining:
                        p.stub_uuid, p.original_id = remaining.pop(0)
                    else:
                        p.origin_stub = stub_uuid
                        p.stub_uuid = _RELEASE_STUB_SENTINEL
                        p.original_id = None
                    break
            for _retracted_uuid, retracted_id in retracted:
                await self._reply_locally(stub_uuid, retracted_id, error=retract_error)
            if forwarder_retracted:
                await self._reply_locally(
                    stub_uuid, forwarder_reply_id, error=retract_error)
            await self._reply_locally(stub_uuid, original_id, result={})
            return True
        parked_replacements = [
            r for r in self._lease_replacement_subscribes.get(uri, [])
            if r[0] == stub_uuid
        ]
        if parked_replacements:
            # This stub parked a replacement subscribe behind an in-flight
            # release and now unsubscribes: leaving the parking in place
            # would let the release drain re-subscribe a stub that has
            # since asked to leave. Retract the parking (committed before
            # any reply is awaited), answer each parked subscribe id with
            # the cancellation error, then fall through — the routing
            # checks below answer the unsubscribe itself truthfully.
            remaining_repl = [
                r for r in self._lease_replacement_subscribes[uri]
                if r[0] != stub_uuid
            ]
            if remaining_repl:
                self._lease_replacement_subscribes[uri] = remaining_repl
            else:
                del self._lease_replacement_subscribes[uri]
            repl_retract_error = {
                "code": _JSONRPC_SERVER_ERROR,
                "message": "resources/subscribe retracted by a later "
                           "unsubscribe",
            }
            for _parked_uuid, parked_id in parked_replacements:
                await self._reply_locally(
                    stub_uuid, parked_id, error=repl_retract_error)
        subscribers = self._resource_subscriptions.get(uri)
        if subscribers is not None and stub_uuid not in subscribers:
            # A stub unsubscribing a URI it never held must not tear down a
            # co-tenant's live lease.
            await self._reply_locally(stub_uuid, original_id, result={})
            return True
        if subscribers is not None and len(subscribers) > 1:
            subscribers.discard(stub_uuid)
            await self._reply_locally(stub_uuid, original_id, result={})
            return True
        if uri in self._lease_awaiting_release:
            # A final release for this URI is already in flight — and it may
            # belong to a stub that has since detached, so a refusal would
            # dead-end this stub: still routed, told to retry by a message
            # nothing acts on. Park behind the one in-flight release instead
            # (mirroring how the grant side parks riders) and settle on its
            # verdict. Forwarding a second release would race two responses
            # against one shared awaiting-release flag. Parkings are counted
            # against the same per-stub cap as subscriptions — a repeated
            # unsubscribe under a slow server must not grow the waiter list
            # without bound.
            parked = sum(
                1
                for waiters in self._lease_release_waiters.values()
                for waiter_uuid, _waiter_id in waiters
                if waiter_uuid == stub_uuid
            )
            if parked >= _RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB:
                # The URI is deliberately NOT logged: resource URIs can
                # carry credentials.
                logger.warning(
                    "backend pid=%s stub=%s at release-waiter cap (%d); "
                    "refusing unsubscribe",
                    self.pid, stub_uuid, _RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB,
                )
                await self._reply_locally(
                    stub_uuid, original_id,
                    error={
                        "code": _JSONRPC_SERVER_ERROR,
                        "message": "resource subscription limit reached",
                    },
                )
                return True
            self._lease_release_waiters.setdefault(uri, []).append(
                (stub_uuid, original_id))
            return True
        if subscribers is None:
            # Nothing tracked for this URI and no release in flight: there
            # is no lease to protect, so the server answers the stray
            # unsubscribe truthfully itself (forwarded unflagged — its
            # response mutates no lease state).
            return False
        # Last subscriber: release the lease upstream. Routing keeps the stub
        # until the server confirms (see _on_resource_subscription_response).
        self._lease_awaiting_release.add(uri)
        return False

    async def _on_resource_subscription_response(
        self, pending: "_PendingRequest", msg: dict[str, Any]
    ) -> None:
        """Apply the lease transition a subscribe/unsubscribe RESPONSE
        confirms or refuses. The wire is one ordered stream, so transitions
        arrive in the order their requests were forwarded.
        """
        uri = pending.resource_uri
        ok = _is_success_response(msg)
        if pending.method == _RESOURCES_SUBSCRIBE_METHOD:
            if self.supports_caller_identity:
                # Per-stub grant: only the caller the server accepted routes.
                if ok:
                    self._resource_subscriptions.setdefault(uri, set()).add(
                        pending.stub_uuid)
                    self._orphaned_leases.discard(uri)
                    if pending.caller is not None:
                        # Grant-time is the only moment the grant principal
                        # is certain; detach releases with this caller.
                        self._grant_callers[(uri, pending.stub_uuid)] = (
                            pending.caller)
                elif "error" not in msg:
                    # UNSETTLED verdict (neither result nor error): the
                    # subscribe may well have taken upstream, and recording
                    # nothing would strand a live per-caller lease firing
                    # updates that route nowhere. Release it as the caller
                    # that took it — the same fail-closed release the
                    # coalesced arms apply to malformed verdicts.
                    await self._release_upstream_subscriptions(
                        [uri], caller=pending.caller)
                return
            # Coalesced grant: the forwarder and every parked rider settle on
            # the server's one verdict — success grants all of them, refusal
            # grants none, and no rider was ever told "subscribed" early.
            self._lease_awaiting_grant.discard(uri)
            riders = self._lease_pending_riders.pop(uri, [])
            if ok:
                # Commit the whole grant BEFORE any reply is awaited: a reply
                # into a full inbox detaches its stub, and when that empties
                # the entry detach deletes it from the table — later adds
                # through the stale set alias would then grant riders into a
                # set the routing table no longer holds.
                granted = self._resource_subscriptions.setdefault(uri, set())
                granted.add(pending.stub_uuid)
                granted.update(rider_uuid for rider_uuid, _ in riders)
                self._orphaned_leases.discard(uri)
                for rider_uuid, rider_id in riders:
                    await self._reply_locally(rider_uuid, rider_id, result={})
                return
            error_obj = msg.get("error")
            # A refusal proves the server holds no lease; a MALFORMED frame
            # (no error either) proves nothing — the subscribe may have
            # taken, and denying routing while keeping it would strand a
            # live lease whose every update is charged to the server as a
            # hazard. An unsettled verdict with nobody routed releases it.
            if error_obj is None and uri not in self._resource_subscriptions:
                await self._release_upstream_subscriptions([uri])
            for rider_uuid, rider_id in riders:
                await self._reply_locally(
                    rider_uuid, rider_id,
                    error=error_obj if isinstance(error_obj, dict) else {
                        "code": _JSONRPC_SERVER_ERROR,
                        "message": "resources/subscribe refused by server",
                    },
                )
            return
        # --- unsubscribe response ---
        if self.supports_caller_identity:
            if ok:
                self._grant_callers.pop((uri, pending.stub_uuid), None)
                subscribers = self._resource_subscriptions.get(uri)
                if subscribers is not None:
                    subscribers.discard(pending.stub_uuid)
                    if not subscribers:
                        del self._resource_subscriptions[uri]
            return
        if uri not in self._lease_awaiting_release:
            return
        self._lease_awaiting_release.discard(uri)
        waiters = self._lease_release_waiters.pop(uri, [])
        if ok:
            # Release confirmed: drop the releasing stub and every parked
            # waiter — all committed BEFORE any reply is awaited. A server
            # may answer concurrent requests out of order, so a replacement
            # subscribe forwarded behind this release can have been granted
            # already — deleting the whole entry would silently discard that
            # fresh grant.
            subscribers = self._resource_subscriptions.get(uri)
            if subscribers is not None:
                subscribers.discard(pending.stub_uuid)
                for waiter_uuid, _waiter_id in waiters:
                    subscribers.discard(waiter_uuid)
                if not subscribers:
                    del self._resource_subscriptions[uri]
            for waiter_uuid, waiter_id in waiters:
                await self._reply_locally(waiter_uuid, waiter_id, result={})
            await self._drain_replacement_subscribes(uri, released=True)
            return
        # On a refusal the server RETAINED the subscription, so routing keeps
        # the entry; the unsubscribing stub receives the server's error and
        # knows its unsubscribe did not take effect — and so does every
        # parked waiter, which stays routed and may retry (the flag is down,
        # so a retry forwards a fresh release).
        error_obj = msg.get("error")
        for waiter_uuid, waiter_id in waiters:
            await self._reply_locally(
                waiter_uuid, waiter_id,
                error=error_obj if isinstance(error_obj, dict) else {
                    "code": _JSONRPC_SERVER_ERROR,
                    "message": "resources/unsubscribe refused by server",
                },
            )
        await self._drain_replacement_subscribes(uri, released=False)

    def resource_subscription_uris(self, stub_uuid: str) -> list[str]:
        """URIs whose routing table names ``stub_uuid`` — the set a
        transparent respawn must replay onto the replacement backend."""
        routed = {
            uri for uri, subscribers in self._resource_subscriptions.items()
            if stub_uuid in subscribers
        }
        # An in-flight replay has not committed routing yet (routing is
        # recorded only on the server's grant): if the replacement dies
        # before its replay responses arrive, those URIs are in neither the
        # routing table nor any pending the next respawn can see — captured
        # from pendings here, or the subscription goes permanently dark.
        routed.update(
            p.resource_uri
            for p in self._pending_requests.values()
            if p.replay_stub == stub_uuid
            and p.method == _RESOURCES_SUBSCRIBE_METHOD
            and p.resource_uri
        )
        # URIs whose replay was in flight when THIS backend died: the
        # backend-gone cleanup preserved them here because clearing the
        # pending table erased their only record.
        routed.update(self._gone_replay_uris.get(stub_uuid, ()))
        return sorted(routed)

    async def replay_resource_subscriptions(
        self, stub_uuid: str, uris: list[str], *,
        caller: Optional[CallerContext] = None,
    ) -> None:
        """Re-establish ``stub_uuid``'s subscriptions on a freshly respawned
        backend, so a live subscription survives the transparent backend swap
        instead of silently going dark (kiro-cli never learns the old backend
        died, so it will never re-subscribe on its own).

        Gateway-originated: each URI is re-subscribed under the lease
        sentinel and the stub's routing grant is recorded only on the
        server's success (a refusal leaves the update undelivered —
        fail-closed — rather than mis-attributed). On an identity-capable
        server the replay carries the connection's authoritative caller
        block, so the server adjudicates it as the same caller that held the
        original subscription; without a caller to inject there the replay is
        skipped entirely, because a bare subscribe would be adjudicated as
        the connection and could be refused — again failing closed.

        A WRITE failure raises :class:`BackendGone` (the replacement's pipe
        is already broken, so the respawn cannot succeed): swallowing it
        would report a successful respawn whose subscriptions are silently
        dark forever. Pendings for URIs written before the failure are
        scoped to release-on-grant — with the respawn failed nobody will
        consume those grants, so each late success releases its lease
        instead of pinning it to a stub the caller is tearing down.
        """
        if self.supports_caller_identity and caller is None:
            logger.debug(
                "backend pid=%s skipping subscription replay for stub=%s: "
                "identity-capable server and no caller context to inject",
                self.pid, stub_uuid,
            )
            return
        replay_generation = self._rekey_generation.get(stub_uuid, 0)
        for uri in uris:
            if self._rekey_generation.get(stub_uuid, 0) != replay_generation:
                # A claim rekeyed this stub mid-replay. The eviction
                # retracted the pendings written so far, but continuing
                # would write the REMAINING URIs under the old owner and
                # route their grants to the new one. Stop here — fail
                # closed, the new owner subscribes on its own.
                logger.debug(
                    "backend pid=%s stopping subscription replay for "
                    "stub=%s: stub was rekeyed mid-replay", self.pid,
                    stub_uuid,
                )
                return
            if stub_uuid in self._resource_subscriptions.get(uri, set()):
                continue
            if not self.supports_caller_identity:
                # Identity-less: the server holds ONE subscription per URI,
                # so a replay flows through the same per-URI lease
                # coordination as a stub subscribe — two uncoordinated
                # replays would put two grants in flight, and a later final
                # unsubscribe would then release the server's single
                # subscription while the other stub stayed locally routed.
                # A replay parking carries no reply id (_reply_locally
                # no-ops on None), so it settles silently on the verdict.
                subscribers = self._resource_subscriptions.get(uri)
                if uri in self._lease_awaiting_grant:
                    self._lease_pending_riders.setdefault(uri, []).append(
                        (stub_uuid, None))
                    continue
                if subscribers is not None and uri not in self._lease_awaiting_release:
                    subscribers.add(stub_uuid)
                    continue
                if uri in self._lease_awaiting_release:
                    self._lease_replacement_subscribes.setdefault(uri, []).append(
                        (stub_uuid, None))
                    continue
                self._lease_awaiting_grant.add(uri)
            fid = self._next_forward_id()
            self._pending_requests[fid] = _PendingRequest(
                stub_uuid=_RELEASE_STUB_SENTINEL, original_id=None,
                method=_RESOURCES_SUBSCRIBE_METHOD, resource_uri=uri,
                replay_stub=stub_uuid,
                caller=caller,
                t_start_ms=time.monotonic() * 1000.0,
            )
            replay_msg: dict[str, Any] = {
                "jsonrpc": "2.0", "id": fid,
                "method": _RESOURCES_SUBSCRIBE_METHOD,
                "params": {"uri": uri},
            }
            if self.supports_caller_identity and caller is not None:
                replay_msg = _inject_caller_meta(replay_msg, caller)
            try:
                await _write_json_line(self.stdin, replay_msg)
            except Exception as exc:
                self._pending_requests.pop(fid, None)
                if not self.supports_caller_identity:
                    # Unwind the lease claim so the URI is not wedged
                    # awaiting a grant that was never written.
                    self._lease_awaiting_grant.discard(uri)
                # Scope any EARLIER replay pendings written in this pass to
                # release-on-grant: their responses may still arrive, and
                # with the respawn reported failed nobody consumes the
                # grants — an attached replay_stub would pin each lease to
                # a stub the caller is about to tear down.
                for p in self._pending_requests.values():
                    if (
                        p.stub_uuid == _RELEASE_STUB_SENTINEL
                        and p.method == _RESOURCES_SUBSCRIBE_METHOD
                        and p.replay_stub == stub_uuid
                    ):
                        p.replay_stub = ""
                # No URI in the line: resource URIs can carry credentials.
                logger.debug(
                    "backend pid=%s could not replay a resource "
                    "subscription (write failed)", self.pid,
                )
                raise BackendGone(
                    "subscription replay write failed on the replacement "
                    "backend"
                ) from exc

    async def _reply_locally(
        self,
        stub_uuid: str,
        original_id: Any,
        *,
        result: Optional[dict[str, Any]] = None,
        error: Optional[dict[str, Any]] = None,
    ) -> None:
        """Answer a stub's request from the gateway without involving the
        backend. A request without an id expects no reply, so none is sent."""
        if original_id is None:
            return
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": original_id}
        if error is not None:
            reply["error"] = error
        else:
            reply["result"] = result if result is not None else {}
        await self._deliver_to_stub(stub_uuid, reply)

    def _stub_subscription_count(self, stub_uuid: str) -> int:
        """Subscription load ``stub_uuid`` holds or is acquiring: confirmed
        table grants, its own forwarded subscribes still awaiting the server,
        and rider parkings — each counted INDIVIDUALLY, duplicates included.
        Grants land on the response, so distinct-URI counting would let a
        stub repeat one URI's subscribe unboundedly while the grant is in
        flight, growing the rider list without limit under a slow server."""
        confirmed = sum(
            1 for subscribers in self._resource_subscriptions.values()
            if stub_uuid in subscribers
        )
        in_flight = sum(
            1 for p in self._pending_requests.values()
            if p.stub_uuid == stub_uuid
            and p.method == _RESOURCES_SUBSCRIBE_METHOD
            and p.resource_uri
        )
        # Subscribes this stub originated that were since converted to the
        # lease sentinel (retracted by an unsubscribe, an eviction, or a
        # detach) but whose server response is still outstanding. Without
        # these a stub cycling subscribe/unsubscribe against an
        # unresponsive server would slip every cycle's pending out of its
        # count and grow the table without bound.
        sentinel_retained = sum(
            1 for p in self._pending_requests.values()
            if p.stub_uuid == _RELEASE_STUB_SENTINEL
            and p.origin_stub == stub_uuid
            and p.method == _RESOURCES_SUBSCRIBE_METHOD
            and p.resource_uri
        )
        parked = sum(
            1
            for riders in self._lease_pending_riders.values()
            for rider_uuid, _rider_id in riders
            if rider_uuid == stub_uuid
        )
        replacement = sum(
            1
            for parkers in self._lease_replacement_subscribes.values()
            for parker_uuid, _parker_id in parkers
            if parker_uuid == stub_uuid
        )
        return confirmed + in_flight + sentinel_retained + parked + replacement

    async def _drain_replacement_subscribes(
        self, uri: str, *, released: bool
    ) -> None:
        """Settle subscribes that parked while ``uri``'s release was in
        flight. On a CONFIRMED release the lease is gone, so the parkers
        take it afresh with ONE forwarded subscribe — first parker as the
        forwarder, the rest parked as riders settling on the same verdict —
        serialized strictly AFTER the release on the wire. On a refusal the
        server RETAINED the lease, so parkers join the still-live entry
        locally (state committed before replies); when detach has pruned the
        entry out from under a retained lease, a fresh subscribe is
        forwarded instead (idempotent on the server's live subscription, and
        its grant re-establishes routing).
        """
        parked = self._lease_replacement_subscribes.pop(uri, [])
        if not parked:
            return
        subscribers = self._resource_subscriptions.get(uri)
        if not released and subscribers is not None:
            subscribers.update(parker_uuid for parker_uuid, _ in parked)
            for parker_uuid, parker_id in parked:
                await self._reply_locally(parker_uuid, parker_id, result={})
            return
        # Take the lease afresh with ONE subscribe forwarded under the
        # sentinel, every parker riding on its verdict: success grants and
        # answers each parker ({} for a stub subscribe, silently for an
        # id-less replay parking), refusal answers each with the server's
        # error. The sentinel arm owns exactly this settle already.
        fid = self._next_forward_id()
        self._pending_requests[fid] = _PendingRequest(
            stub_uuid=_RELEASE_STUB_SENTINEL, original_id=None,
            method=_RESOURCES_SUBSCRIBE_METHOD, resource_uri=uri,
            t_start_ms=time.monotonic() * 1000.0,
        )
        self._lease_awaiting_grant.add(uri)
        self._lease_pending_riders.setdefault(uri, []).extend(parked)
        try:
            await _write_json_line(self.stdin, {
                "jsonrpc": "2.0", "id": fid,
                "method": _RESOURCES_SUBSCRIBE_METHOD,
                "params": {"uri": uri},
            })
        except Exception:
            # Backend going away: unwind and answer every parker — an id
            # silently dropped from the tables hangs in the client forever.
            self._pending_requests.pop(fid, None)
            self._lease_awaiting_grant.discard(uri)
            remaining = [
                r for r in self._lease_pending_riders.get(uri, [])
                if r not in parked
            ]
            if remaining:
                self._lease_pending_riders[uri] = remaining
            else:
                self._lease_pending_riders.pop(uri, None)
            err = {
                "code": _JSONRPC_SERVER_ERROR,
                "message": "resources/subscribe could not be forwarded "
                           "(backend going away)",
            }
            for parker_uuid, parker_id in parked:
                await self._reply_locally(parker_uuid, parker_id, error=err)

    async def _release_upstream_subscriptions(
        self, uris: list[str], *, caller: Optional[CallerContext] = None,
    ) -> None:
        """Send a gateway-originated ``resources/unsubscribe`` for each URI
        whose last subscriber departed without unsubscribing. Best-effort: a
        backend that is already dying takes its subscriptions with it.

        On an identity-capable server pass the caller that HOLDS the grant:
        the release is adjudicated per principal, so a bare unsubscribe
        (adjudicated as the connection) would be refused — or worse, revoke
        a different principal's live subscription.
        """
        for uri in uris:
            fid = self._next_forward_id()
            self._pending_requests[fid] = _PendingRequest(
                stub_uuid=_RELEASE_STUB_SENTINEL, original_id=None,
                method=_RESOURCES_UNSUBSCRIBE_METHOD, resource_uri=uri,
                t_start_ms=time.monotonic() * 1000.0,
            )
            if not self.supports_caller_identity:
                # Mark the release in flight BEFORE writing: without the
                # flag a replacement subscribe takes the first-subscriber
                # path and forwards BESIDE this release, racing its answer
                # order — the same silent update loss the stub-forwarded
                # release path parks against. The sentinel response arm
                # clears the flag and drains any parked replacements on the
                # server's verdict. Identity-capable servers never coalesce
                # (every subscribe forwards per caller), so the flag is not
                # consulted there and setting it would only go stale.
                self._lease_awaiting_release.add(uri)
            release_msg: dict[str, Any] = {
                "jsonrpc": "2.0", "id": fid,
                "method": _RESOURCES_UNSUBSCRIBE_METHOD,
                "params": {"uri": uri},
            }
            if self.supports_caller_identity and caller is not None:
                release_msg = _inject_caller_meta(release_msg, caller)
            try:
                await _write_json_line(self.stdin, release_msg)
            except Exception:
                # Per-URI, not terminal: one failed write must not leak every
                # remaining server-side lease (the pipe may also be gone, in
                # which case the dying backend takes them all anyway).
                self._pending_requests.pop(fid, None)
                self._lease_awaiting_release.discard(uri)
                # The lease is now RETAINED upstream through the broker's
                # own failure: mark it orphaned so its later updates are
                # dropped without charging a hazard to a blameless server.
                while len(self._orphaned_leases) >= _ORPHANED_LEASES_MAX:
                    self._orphaned_leases.pop()
                self._orphaned_leases.add(uri)
                # No URI in the line: resource URIs can carry credentials.
                logger.debug(
                    "backend pid=%s could not release a resource "
                    "subscription (backend going away)", self.pid,
                )
                continue

    def _resource_update_targets(self, msg: dict[str, Any]) -> set[str]:
        """Return the stubs subscribed to an incoming
        ``notifications/resources/updated``'s URI — empty when the URI is
        missing, malformed, or has no subscribers (the caller then applies
        the deny-by-default drop).

        Returns a COPY: delivery awaits per stub, and a full inbox detaches
        its stub mid-fan-out, which mutates the underlying table.
        """
        params = msg.get("params")
        if not isinstance(params, dict):
            return set()
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            return set()
        return set(self._resource_subscriptions.get(uri, ()))

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

        The tools/list visibility filter runs even when the feature gate is
        OFF — see below for why — so this is not a pure no-op in that state.
        """
        # App-originated callbacks (the app-call relay forwards a tools/call on
        # a ``__app_call__*`` stub) must NEVER be re-intercepted: if the called
        # tool itself declares a ui:// resource, re-spooling would replace the
        # app's real result with an internal marker string and mint a stray
        # spool record. The render/spool path is only for MODEL-originated tool
        # results; app callbacks return verbatim to the requesting app.
        #
        # Checked ahead of the feature gate because it also exempts a listing
        # from the visibility filter, which runs gate-independently.
        if pending.stub_uuid.startswith("__app_call__"):
            return False
        if pending.method == "tools/list":
            result = msg.get("result")
            if isinstance(result, dict):
                # SEP-1865 MUST: a tool whose visibility omits "model" is not
                # the agent's to see. Mutates the response in place before the
                # caller delivers it. Reachable ONLY for model-facing listings —
                # the __app_call__ guard above returns first, so an app's
                # authorization snapshot keeps its app-only tools and the
                # visibility gate in app_call still sees them.
                #
                # DELIBERATELY OUTSIDE the feature gate: visibility is the
                # SERVER's statement about who may call a tool, not a property
                # of our renderer. With apps disabled there is no app to call
                # an app-only tool, so filtering makes it unreachable — which is
                # the server's own consequence and strictly better than handing
                # the model a tool the server withheld from it.
                hidden = strip_model_hidden_tools(result)
                if hidden.declared:
                    logger.info(
                        "mcp-apps: withheld %d app-only tool(s) from the agent's "
                        "listing for server=%s: %s",
                        len(hidden.declared), self.pool_key.server_name,
                        ", ".join(hidden.declared),
                    )
                if hidden.unreadable:
                    # WARNING, not INFO: the server DID declare a visibility and
                    # this host could not parse it, so a tool disappeared on our
                    # judgement rather than the server's instruction. That is the
                    # one drop an operator needs to see — it is the failure mode
                    # where a real server's shape trips the parser.
                    logger.warning(
                        "mcp-apps: withheld %d tool(s) from the agent's listing "
                        "for server=%s because their _meta.ui.visibility could "
                        "not be read: %s",
                        len(hidden.unreadable), self.pool_key.server_name,
                        ", ".join(hidden.unreadable),
                    )
                if hidden:
                    self._audit_visibility_withhold(pending, hidden)
                # Passive harvest: tool declarations are the SEP-1865 PRIMARY
                # place a server associates a tool with its ui:// resource (the
                # real pdf-server and Excalidraw declare it ONLY here, not on
                # results). Every tools/list response updates the map.
                #
                # Runs AFTER the strip, so a withheld tool's ui:// never enters
                # the map. That ordering is load-bearing, not incidental: it
                # means a model-originated call naming an app-only tool cannot
                # find a declared resource to render. Keep the strip first.
                #
                # Also runs REGARDLESS of the feature gate, so the map always
                # reflects the server's CURRENT declarations. Gating it would
                # let a listing that arrives while apps are disabled leave a
                # WITHDRAWN tool→ui association cached, which a later re-enable
                # would then render from.
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
        # Everything below is the RENDER path, which the feature gate owns.
        if not _mcp_apps_enabled():
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

    def _audit_visibility_withhold(
        self, pending: _PendingRequest, hidden: WithheldTools
    ) -> None:
        """SEL-audit a tools/list visibility withhold.

        Removing a tool from the agent's listing is an authorization decision
        this gateway makes, and the sibling direction (an app calling a tool,
        in :mod:`kiro_crew.mcp_gateway.app_call`) audits every outcome — so the
        direction that silently takes capability AWAY from the model must not
        be the unaudited one. Log lines rotate; the SEL chain is the durable
        record of what was hidden and why.

        ONE event per listing that actually withheld something, not one per
        tool and not one per tools/list — a server with a permanent app-only
        tool would otherwise mint an event on every listing forever.
        """
        try:
            SecurityEventLog().log_api_access(
                caller=pending.session_key or "unknown",
                operation="mcp-gateway.tools-list-visibility",
                outcome="denied",
                source="gateway",
                resources=(
                    f"server={self.pool_key.server_name} "
                    f"declared={','.join(hidden.declared) or '-'} "
                    f"unreadable={','.join(hidden.unreadable) or '-'}"
                ),
            )
        except Exception:  # pragma: no cover — audit must never break delivery
            logger.debug(
                "SEL audit for tools/list visibility withhold failed", exc_info=True
            )

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
        except (ValueError, UnicodeDecodeError):
            pass
        # If prefix-parse failed, try a targeted regex for "id": value.
        if msg_id is None:
            prefix_str = raw[:256].decode("utf-8", errors="replace")
            m = re.search(r'"id"\s*:\s*("(?:[^"\\]|\\.)*?"|\d+|null)', prefix_str)
            if m:
                try:
                    msg_id = json.loads(m.group(1))
                except ValueError:
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
        # Disarmed rather than awaited: teardown is reachable from inside the
        # deadline's own coroutine (it calls shutdown), and awaiting the
        # current task would deadlock. _cancel_init_deadline skips that case.
        self._cancel_init_deadline()
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
        # Deliberately NO temp sweep here: the launcher's exit is not proof
        # its process TREE is gone (a wrapper launcher exits while its server
        # child lives on and keeps using the dir). The single deletion
        # authority is backend_tmp.sweep_all_backend_tmp, which requires the
        # recorded owner to be dead AND the directory to be idle.


# --- Spawn / handshake ------------------------------------------------------


async def spawn_backend(
    pool_key: "PoolKey",
    command: str,
    args: list[str],
    env: Mapping[str, str],
    work_dir: str,
    declared_temp_keys: tuple[str, ...] = (),
) -> Backend:
    """Spawn a real MCP subprocess and wrap it in a :class:`Backend`.

    ``declared_temp_keys`` are the temp-key names (``TMPDIR``/``TMP``/``TEMP``,
    any casing) the operator's agent spec DECLARES for this server -- the
    caller knows the declared-env set and this function does not (``env``
    also carries the daemon's ambient values, which must not suppress
    containment; see the containment block below).

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
    # Per-process temp containment (#5064). Safe re: the pooled-backend
    # PoolKey invariant for the same reason as the marker above -- the value
    # is derived from the key's own digest plus a token generated AFTER
    # pool-identity resolution and is never folded into the hash, so it can
    # neither split nor collapse pool identity. Allocated off-loop (mkdir is
    # filesystem work), and fail-open: containment is hygiene, not a spawn
    # prerequisite -- a host where the dir cannot be created still gets a
    # working backend with today's inherited-temp behavior.
    #
    # An OPERATOR-DECLARED temp wins: a spec that sets any of TMPDIR/TMP/TEMP
    # deliberately points a heavy server at chosen storage (e.g. a capacity
    # volume), and overriding it would trade litter for ENOSPC. No allocation
    # happens at all in that case -- no empty dir, nothing to sweep.
    #
    # Declaration is signalled by the CALLER (``declared_temp_keys``), not
    # read off ``spawn_env``: the resolver folds the daemon's own inherited
    # environment into ``env``, and macOS always exports TMPDIR (Windows
    # always exports TMP/TEMP), so an env-membership test would read the
    # ambient value as a declaration and silently disable containment on
    # those platforms. Ambient keys are OVERRIDDEN by the managed triple on
    # success, left untouched on allocation failure (the documented fail-open
    # "inherited temp" fallback), and PRUNED down to the declared set when
    # the operator declared a temp (see the else branch).
    backend_tmp: Optional[Path] = None
    _declared_upper = {key.upper() for key in declared_temp_keys}
    if not _declared_upper:
        try:
            backend_tmp = await asyncio.to_thread(
                allocate_backend_tmp, pool_key.stable_hash()
            )
            spawn_env.update(tmp_env(backend_tmp))
        except OSError:
            logger.warning(
                "backend-tmp: could not allocate a contained temp dir; spawning "
                "with inherited temp",
                exc_info=True,
            )
    else:
        # Yielding is not enough on its own: the daemon's AMBIENT temp keys
        # are still in ``spawn_env``, and ``tempfile`` consults TMPDIR before
        # TMP -- a spec declaring only ``TMP`` on macOS would silently write
        # through the inherited ambient TMPDIR. Strip the canonical keys the
        # operator did NOT declare so the declared one actually governs.
        for key in ("TMPDIR", "TMP", "TEMP"):
            if key not in _declared_upper:
                spawn_env.pop(key, None)
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
    if backend_tmp is not None:
        # Liveness anchor for the daemon-boot sweep: a SIGKILL'd daemon can
        # leave this backend running (start_new_session), and the next boot
        # must not delete temp storage under a survivor. Off-loop, fail-open
        # (an unowned dir falls under the sweep's grace-window rule instead).
        await asyncio.to_thread(record_owner, backend_tmp, process.pid)
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
