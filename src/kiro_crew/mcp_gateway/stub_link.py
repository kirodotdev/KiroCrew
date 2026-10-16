"""The stub's link to the gateway daemon: opening it, judging it, and opening it again.

Owner module of :mod:`kiro_crew.mcp_gateway.stub`, which re-exports every name here;
read and patch them there. This module owns:

* the Register handshake (:func:`handshake`) and the read ceiling the stub was launched
  with, which sizes that connection's reader (:func:`_adopt_read_ceiling`). The ceiling
  and the two hold bounds derived from it are rebound at run time, so they are defined
  here, beside every reader of them, and nothing else binds them;
* the checks on what a daemon advertised that send a stub to its fallback instead
  (``must_degrade_*``), and the errors a handshake raises (:class:`FallbackRequestedError`,
  :class:`StaleGenerationError`);
* the admission pre-flight before any MCP traffic (:func:`_ensure_backend_admitted`), the
  typed refusal kiro-cli gets when the daemon is out of capacity
  (:func:`_serve_capacity_refusal`), and the ``initialize`` a reconnect replays
  (:func:`_replay_initialize`);
* the reconnect after a live connection dies: its budget, its backoff, and the bounded
  hold of kiro-cli's requests while the link is down (:func:`_reconnect`,
  :func:`_drain_while_disconnected`, :func:`_reconnect_while_draining`).

It logs under the stub's logger, defined in :mod:`kiro_crew.mcp_gateway.stub_bridge`. It never imports the facade.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable, Optional

from kiro_crew.json_line import parse_json_object_line
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.read_limits import (
    _DEFAULT_READ_BUFFER_LIMIT,
    read_buffer_limit_from_flag,
)
from kiro_crew.mcp_gateway.shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS
from kiro_crew.mcp_gateway.stub_bridge import (
    _BRIDGE_KEEPALIVE_TYPE,
    _BRIDGE_PING_INTERVAL_SECS,
    _BRIDGE_PING_MAX_MISSES,
    _BRIDGE_PONG_TYPE,
    _ERROR_EMIT_TIMEOUT_SECS,
    _SPAWN_QUEUED_TYPE,
    _STDIN_QUEUE_MAXSIZE,
    _emit_error_frames,
    _NonObjectFrame,
    _read_frame,
    _safe_close,
    _write_frame,
    logger,
)

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.stub_bridge import StubSession


_HANDSHAKE_TIMEOUT_SECS = 3.0


# --- Reconnect budget -------------------------------------------------------
# What the stub spends trying to re-attach after a live gateway connection dies
# mid-session. The daemon is supervised and respawns itself with its own
# exponential backoff, so one connect attempt would lose the race against an
# ordinary restart.
#
# The two outcomes are NOT symmetric, and the budget is sized by that asymmetry:
#
# * Giving up is IRREVERSIBLE for the session. The terminal exit closes stdout,
#   kiro-cli logs ``Transport to MCP server ... is closed`` and never re-mounts
#   a closed server, so every later call to these tools hangs or fails for the
#   rest of a chat session that may live for hours. The only recovery is a new
#   session -- the user has to notice and act.
# * Waiting is RECOVERABLE. While the stub retries, the stdio transport to
#   kiro-cli stays open and newly arriving requests are buffered (see
#   ``StubSession.next_line``) for ``_QUEUED_GRACE_SECS``, long enough that an
#   ordinary restart serves them for real; past that each one is failed with the
#   same retryable ``-32603`` a call already in flight gets, so the SESSION
#   keeps waiting while no individual CALL waits longer than the live bridge
#   would have let it (see :func:`_drain_while_disconnected`). Under overload the
#   system queues and waits -- it trades time for capacity -- rather than dying.
#
# So the budget has to outlast not one supervisor respawn but a short CRASH LOOP
# of them: a supervisor that kills and respawns gatewayd ten times inside half an
# hour leaves the endpoint absent for around ten minutes, and a budget of one
# minute takes the terminal exit there, so every long-lived session loses
# kirocrew-core for the rest of its life. One kill->respawn cycle, by the
# supervisor's own constants:
#
#   detection   ``manager._LIVENESS_PING_INTERVAL_SECS`` (30)
#               x ``manager._LIVENESS_MAX_CONSECUTIVE_FAILURES`` (3)  =  90s
#   shutdown    ``TOTAL_SHUTDOWN_BUDGET_SECS`` (SIGTERM->SIGKILL grace) =  20s
#   backoff     ``manager._RESPAWN_BACKOFF_MAX_SECS``                  =  60s
#   start       cold daemon start until the endpoint is bound          ~  10s
#                                                                        ----
#                                                                        180s
#
# Three such cycles is 540s; the budget is rounded up to 600s. The manager
# values are MIRRORED here by name rather than imported: ``manager`` pulls in
# ``config.paths``, ``sandbox`` and ``code_fingerprint``, which is far more than
# the stub's cold-start import budget allows. ``TOTAL_SHUTDOWN_BUDGET_SECS``
# lives in a stdlib-only leaf and IS imported. ``test_stub_reconnect_budget.py``
# pins the mirrors to the manager's real values so they cannot drift silently.
#
# The budget is still finite: a gateway that is gone for good must eventually
# reach the terminal exit rather than leave the session parked on a socket
# nobody will bind again. It is also not a cover for every conceivable recovery:
# a respawn that keeps failing backs off to 60s per retry with no attempt cap, so
# a pathological gateway can still outlast this. The trade is deliberate -- past
# ten minutes the honest signal to a waiting session is that its tools are gone,
# not more silence.
#
# The deadline bounds when new ATTEMPTS stop, not the exit itself: an attempt
# already under way when it passes runs to its own end, so the exit can trail the
# budget by up to the handshake (``_HANDSHAKE_TIMEOUT_SECS``) plus the replay
# (``_REPLAY_INIT_TIMEOUT_SECS``) plus one backoff step
# (``_RECONNECT_BACKOFF_MAX_SECS``) -- about 43s today; the queue-aware
# pre-flight in between is clipped to the budget still remaining, so it adds
# nothing of its own. That is deliberate: abandoning a handshake that is
# mid-replay would throw away the most likely successful attempt in exchange for
# meeting a number exactly.
_SUPERVISOR_LIVENESS_PING_INTERVAL_SECS = 30.0  # manager._LIVENESS_PING_INTERVAL_SECS
_SUPERVISOR_LIVENESS_MAX_FAILURES = 3  # manager._LIVENESS_MAX_CONSECUTIVE_FAILURES
_SUPERVISOR_RESPAWN_BACKOFF_MAX_SECS = 60.0  # manager._RESPAWN_BACKOFF_MAX_SECS
_SUPERVISOR_COLD_START_SECS = 10.0  # daemon exec -> endpoint bound, generous
_SUPERVISOR_RESPAWN_CYCLE_SECS = (
    _SUPERVISOR_LIVENESS_PING_INTERVAL_SECS * _SUPERVISOR_LIVENESS_MAX_FAILURES
    + TOTAL_SHUTDOWN_BUDGET_SECS
    + _SUPERVISOR_RESPAWN_BACKOFF_MAX_SECS
    + _SUPERVISOR_COLD_START_SECS
)
#: How many back-to-back kill->respawn cycles the reconnect must outlast.
_RECONNECT_CRASH_LOOP_CYCLES = 3
_RECONNECT_BACKOFF_START_SECS = 0.5
# Capped at 10s so a ten-minute budget polls the socket ~60 times rather than
# ~150: enough not to hammer a daemon that is trying to come up, still fast
# enough that a session re-attaches within seconds of the endpoint binding.
_RECONNECT_BACKOFF_MAX_SECS = 10.0
# How long a request that ARRIVES while the bridge is down may sit unanswered
# before it is failed retryably. The budget above is how long the SESSION waits;
# this is how long any one CALL waits, and the two are separate on purpose: a
# session that gives up loses its tools irreversibly, while a call that is failed
# retryably costs one retry.
#
# Deliberately the same figure a LIVE bridge already allows a silent gateway
# (``_BRIDGE_PING_INTERVAL_SECS`` x ``_BRIDGE_PING_MAX_MISSES``), so the two
# waits agree: a queued call is not held longer than a forwarded one would be
# before the peer is declared dead. Sizing it from that constant rather than
# picking a number also means it cannot drift away from the patience the rest of
# the stub shows a slow daemon.
#
# Short enough to matter: an ordinary restart re-attaches inside it and the call
# is served for real, so the error path is reached only by the crash loop the
# budget exists for. Long enough to matter: it is not zero, because failing a
# call the next second would have answered turns a recoverable hiccup into a
# visible tool error for no gain.
_QUEUED_GRACE_SECS = _BRIDGE_PING_INTERVAL_SECS * _BRIDGE_PING_MAX_MISSES


# Byte bounds on what ``_drain_while_disconnected`` RETAINS. Both dimensions are
# load-bearing, the same pair and the same reasoning as
# ``gatewayd._MAX_PENDING_FRAMES`` / ``_MAX_PENDING_BYTES``: a count bound alone
# admits ``_STDIN_QUEUE_MAXSIZE`` x the largest line the transport will carry, and
# a byte bound alone leaves the per-object overhead of very many tiny frames
# unaccounted. The aggregate follows the per-frame ceiling upward so one frame the
# reader was willing to return can never trip it alone, and is floored at the
# SHIPPED read limit so tuning ``mcp_gateway.read_buffer_limit_bytes`` down (1 KiB
# is accepted) cannot tighten the hold along with it.
#
# All three start at the SHIPPED default and are replaced by
# :func:`_adopt_read_ceiling` before the stub opens anything. The default is a
# plain integer from a stdlib-only leaf, so this module's body still reads no
# config -- which is the whole point, since a config read costs roughly 140
# modules in a process that exists once per session per MCP server.
READ_BUFFER_LIMIT_BYTES = _DEFAULT_READ_BUFFER_LIMIT
_HELD_FRAME_BYTES = READ_BUFFER_LIMIT_BYTES
_HELD_TOTAL_BYTES = max(_DEFAULT_READ_BUFFER_LIMIT, _HELD_FRAME_BYTES)


def _adopt_read_ceiling(args: argparse.Namespace) -> int:
    """Bind the read ceiling this stub was LAUNCHED with, before it connects.

    The rewriter resolves the ceiling when it writes the overlay -- it has
    config loaded anyway -- and stamps the answer into ``--read-limit``.
    Adopting it off argv is what keeps a running stub out of the config package.

    Called from the entry point, because a launch parameter is adopted once, by
    whoever read argv. A stub whose overlay predates the flag adopts nothing
    usable and :func:`read_buffer_limit_from_flag` resolves the long way, so the
    precedence an operator sees -- env var, then the config key, then the
    default -- is the same either way.

    Writes the two retention bounds as well, so every reader of them sees one
    answer. ``_HELD_TOTAL_BYTES`` is floored at the SHIPPED default on purpose:
    tuning the key DOWN (1 KiB is accepted) must not tighten what a disconnected
    stub may hold, or a reconnect would drop frames it could have replayed.
    """
    raw = getattr(args, "read_limit", None)
    flag_value: Optional[int] = None
    if raw is not None:
        try:
            flag_value = int(raw)
        except (TypeError, ValueError):
            logger.warning("ignoring unparsable --read-limit %r", raw)
    value = read_buffer_limit_from_flag(flag_value)
    globals()["READ_BUFFER_LIMIT_BYTES"] = value
    globals()["_HELD_FRAME_BYTES"] = value
    globals()["_HELD_TOTAL_BYTES"] = max(_DEFAULT_READ_BUFFER_LIMIT, value)
    return value


# Coupled to the DEFAULT of ``mcp_gateway.spawn_queue_wait_secs``
# (``config/integration_sections.py``): the daemon holds a queued stub for about that long
# before a capacity refusal, and this budget is how long the stub keeps
# kiro-cli's transport open meanwhile. The stub reads no config, so the coupling
# is by value; ``test_stub_reconnect_budget.py`` pins the equality, and
# >= _RECONNECT_CRASH_LOOP_CYCLES * _SUPERVISOR_RESPAWN_CYCLE_SECS. Neither
# direction of drift costs a refusal, because THIS number is what the daemon
# waits on: it is sent as ``wait_budget_secs``, the daemon caps it by its own key
# and then subtracts a margin (``gatewayd._QUEUE_REFUSAL_MARGIN_SECS``) so its
# wait always ends inside this one. Whatever the key says, the refusal arrives
# while this stub is still listening -- which is what keeps it from exec'ing a
# backend on a host the daemon just refused one for.
_RECONNECT_TOTAL_BUDGET_SECS = 600.0
# Bounds the replayed ``initialize`` on a fresh connection. The daemon answers
# it either from its init cache or by driving a real upstream handshake, so this
# has to cover a cold backend spawn; on timeout the reconnect is abandoned and
# the stub takes the terminal exit.
_REPLAY_INIT_TIMEOUT_SECS = 30.0
# Pre-flight ``ensure_backend`` reply timeout. Must comfortably
# exceed cold backend fork latency. On timeout the stub falls back to a
# direct per-session exec, so an over-generous value only costs a slower
# recovery when the gateway is genuinely wedged.
_ENSURE_BACKEND_TIMEOUT_SECS = 25.0
# Queue-aware pre-flight (daemon advertised ``spawn_queue``): the same 25 s is
# a SILENCE window, renewed by every ``queued`` / ``keepalive`` / ``pong``
# control frame the daemon sends while this stub waits in its spawn gate, and
# the whole wait is bounded by ``_RECONNECT_TOTAL_BUDGET_SECS`` -- the one
# figure this stub already promises to hold kiro-cli's transport open for.
_SPAWN_QUEUE_SILENCE_SECS = _ENSURE_BACKEND_TIMEOUT_SECS
_SPAWN_QUEUE_WAIT_BUDGET_SECS = _RECONNECT_TOTAL_BUDGET_SECS


# ``rejected`` classes a stub may act on. Only the two target-shaped classes
# ever lead to ``fallback_exec``; ``capacity`` is answered to kiro-cli as a
# typed JSON-RPC error with the transport left open (see
# ``_serve_capacity_refusal``). A frame with no ``class`` came from an older
# daemon and keeps the pre-class rules.
_REJECT_CLASS_CAPACITY = "capacity"
_REJECT_CLASS_COMPAT = "compat"
_REJECT_CLASS_ISOLATION = "isolation"
_FALLBACK_CLASSES = frozenset({_REJECT_CLASS_COMPAT, _REJECT_CLASS_ISOLATION})
#: JSON-RPC error code kiro-cli receives for a request the gateway refused for
#: capacity. Server-defined range; ``data.class`` carries the rejection class
#: and ``data.retry_after_secs`` the daemon's hint.
_CAPACITY_ERROR_CODE = -32001


#: Invariant fragment of gatewayd's ``_TargetUnknown`` message. This is a FROZEN
#: WIRE CONTRACT, not an implementation detail to be tidied: the daemon that
#: sends it may be an old surviving process whose text can never be changed, so
#: matching is the only classifier available for it. Only the stable leading
#: clause is matched -- the remedy clause after it names an env prefix that HAS
#: already changed across versions (``MC_MCP_TARGET_`` -> ``KIROCREW_MCP_TARGET_``).
#: A test pins this against the live gatewayd string so the two cannot drift.
TARGET_UNKNOWN_REASON = "no target mapping for server"


def must_degrade_unknown_target(reason: str) -> bool:
    """Should an UNTAGGED rejection still be treated as fallback-eligible?

    A current daemon tags an unknown target ``fallback: true`` and this never
    fires. It exists for the daemon that CANNOT tag it, which is precisely the
    population the tag was added for: ``GatewayManager`` adopts any process
    answering ``pong`` with no version handshake, so a daemon that outlived a
    package upgrade serves brand-new stubs while predating every wire field
    added since. Its target map is frozen at its own spawn, so it is also the
    daemon most likely to be missing a server -- the tag would arrive exactly
    where it cannot be sent.

    Without this the PR's own fix is inert for the reported case: upgrade, old
    daemon survives, adoption, untagged target-unknown, stub exits, and the
    server's whole tool surface disappears from the session anyway.

    Deliberately narrow. It matches ONLY the unknown-target class, so a genuine
    spawn failure stays terminal and does not become a per-session crash-loop --
    which is the reason the terminal path exists. Same shape as
    :func:`must_degrade_unshareable`: the stub cannot verify the daemon's
    version, so it reasons from what the daemon said.
    """
    return TARGET_UNKNOWN_REASON in (reason or "")


def must_degrade_unshareable(poolable: bool, capabilities: list[str]) -> bool:
    """Should this connection abandon the gateway rather than risk being pooled?

    True only when the stub asked for a PRIVATE backend and the daemon did not
    attest that it reads the ``poolable`` register field. Such a daemon ignores
    the flag and routes the register through the shared index, so a server the
    operator never allowlisted would be silently co-tenanted — and a stub cannot
    detect that after the fact. Degrading gives the per-session exec, which is
    the private topology it asked for.

    A stub that DID ask to share needs no attestation: an older daemon pools it,
    which is what it wanted.
    """
    return not poolable and "poolable_ack" not in capabilities


def must_degrade_nonce_blind(server: str, poolable: bool, capabilities: list[str]) -> bool:
    """Should this connection abandon the gateway rather than pool without a nonce?

    The mirror of :func:`must_degrade_unshareable`. That one guards the stub that
    asked for a PRIVATE backend; this one guards the stub that asked for a POOLED
    one, and only for the servers in
    :data:`~kiro_crew.mcp_caller.POOLING_REQUIRES_TENANT_NONCE`. Such a backend
    keeps per-tenant state for callers the gateway cannot name and separates it by
    the per-connection nonce, so one pooled process serving several unnamed
    connections holds a single namespace for all of them as soon as no nonce
    arrives.

    Nothing downstream can catch that. At the backend an absent tenant block is
    equally what a 1:1 topology with no gateway looks like, where the per-process
    fallback separates sessions exactly as far as they really are separate. The
    handshake is the one place the two are distinguishable, because the daemon
    actually serving the frames says whether it mints a nonce.

    Degrading is not a consolation prize: an exclusive backend is the topology in
    which that per-process fallback is correct, so the separation the pooled path
    would have lost is restored rather than approximated.

    Scoped to the named servers deliberately. Every other pooled stub is unharmed
    by a nonce-blind daemon, and degrading all of them would spend one process per
    session on any host whose daemon outlived a package upgrade.
    """
    from kiro_crew.mcp_caller import POOLING_REQUIRES_TENANT_NONCE

    if server not in POOLING_REQUIRES_TENANT_NONCE:
        return False
    return poolable and "tenant_nonce" not in capabilities


class FallbackRequestedError(Exception):
    """Handshake cannot complete; caller must exec the real backend."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StaleGenerationError(FallbackRequestedError):
    """The daemon answered the Register with no code fingerprint, or another one.

    A :class:`FallbackRequestedError`, so the cold-start caller degrades to its
    per-session ``fallback_exec`` exactly as for any other handshake failure.
    Distinguished so the reconnect path can tell it from an outage: a daemon that
    is absent or still starting is retried, but one that is UP and of a different
    generation will answer the next attempt the same way, so retrying it only
    spends the whole reconnect budget on registers the stub then closes.
    """


async def handshake(
    socket_path: str, payload: dict
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str, dict]:
    """Connect + Register. Raises :class:`FallbackRequestedError` for any
    gateway-unavailable condition (connect refused, socket missing,
    rejected register, unexpected reply)."""
    try:
        reader, writer = await transport.connect(
            socket_path,
            limit=READ_BUFFER_LIMIT_BYTES,
        )
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        raise FallbackRequestedError(f"connect failed: {exc}") from exc

    try:
        await _write_frame(writer, payload)
        resp = await _read_frame(reader)
    except (OSError, ConnectionError, json.JSONDecodeError) as exc:
        await _safe_close(writer)
        raise FallbackRequestedError(f"register io failed: {exc}") from exc

    if resp is None:
        await _safe_close(writer)
        raise FallbackRequestedError("gateway closed during handshake")

    msg_type = resp.get("type")
    if msg_type == "registered":
        expected_generation = payload.get("stub_code_fingerprint")
        if isinstance(expected_generation, str) and expected_generation:
            daemon_generation = resp.get("fingerprint")
            if not isinstance(daemon_generation, str) or not daemon_generation:
                await _safe_close(writer)
                raise StaleGenerationError(
                    "gateway did not report its code fingerprint; using direct execution"
                )
            if daemon_generation != expected_generation:
                await _safe_close(writer)
                raise StaleGenerationError(
                    "gateway code fingerprint does not match this stub; using direct execution"
                )
        return reader, writer, payload["stub_uuid"], resp
    await _safe_close(writer)
    if msg_type == "rejected":
        raise FallbackRequestedError(f"gateway rejected: {resp.get('reason', '?')}")
    raise FallbackRequestedError(f"unexpected handshake reply: type={msg_type!r}")


#: Replay outcomes. The distinction is the whole point: a REFUSAL will answer the
#: same way on every later attempt, so retrying it only delays the terminal exit;
#: a transport loss says nothing about the answer, and giving up on one would
#: waste the reconnect budget in exactly the case it exists for -- a supervisor
#: respawn, where the daemon this stub reaches may still be starting up or may
#: itself die again mid-replay.
_REPLAY_OK = "ok"
_REPLAY_RETRY = "retry"
_REPLAY_REFUSE = "refuse"


async def _replay_initialize(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    session: StubSession,
) -> tuple[str, Optional[bytes], str]:
    """Re-establish the MCP handshake for a reconnected stub.

    kiro-cli sends ``initialize`` exactly once and never repeats it, and the
    daemon held the captured copy in per-connection memory that died with it. So
    the stub replays its own copy: the fresh daemon either answers from a
    backend's init cache or drives a real upstream handshake, and either way the
    connection ends up in the state the session already believes it is in.
    Replaying more than once is safe for the same reason -- a backend that is
    already handshake-complete answers a later stub from that cache.

    Returns ``(status, forward, detail)`` where status is one of
    :data:`_REPLAY_OK`, :data:`_REPLAY_RETRY` or :data:`_REPLAY_REFUSE`.
    ``forward`` is a line the caller must write to kiro-cli -- non-empty ONLY
    when the original handshake never got its answer, in which case this reply is
    the one kiro-cli is still waiting for. When the session DID get an answer,
    the reply is compared against it and swallowed: kiro-cli already holds a
    response for that id, and a second one for the same id is a protocol
    violation.
    """
    if session.captured_init is None:
        return _REPLAY_REFUSE, None, "no_captured_initialize"
    init_id = session.captured_init_id()
    try:
        await _write_frame(writer, json.loads(session.captured_init))
    except (OSError, ConnectionError) as exc:
        return _REPLAY_RETRY, None, f"replay_write_failed: {exc}"
    except (ValueError, TypeError) as exc:
        # Our own captured frame does not parse; no connection will fix that.
        return _REPLAY_REFUSE, None, f"captured_initialize_malformed: {exc}"

    async def _await_reply() -> tuple[str, Optional[bytes], str]:
        while True:
            line = await reader.readuntil(b"\n")
            if not line:
                return _REPLAY_RETRY, None, "closed_during_replay"
            msg = parse_json_object_line(line, errors="replace")
            if msg is None:
                continue
            if msg.get("type") in (_BRIDGE_PONG_TYPE, _BRIDGE_KEEPALIVE_TYPE, _SPAWN_QUEUED_TYPE):
                continue
            if msg.get("id") != init_id or "method" in msg:
                # Anything else on a connection whose only traffic so far is our
                # own replay is not addressed to this session's handshake.
                continue
            if "error" in msg:
                return _REPLAY_REFUSE, None, f"replay_rejected: {msg['error']}"
            if session.init_result is None:
                # The first handshake never completed, so kiro-cli is still
                # waiting: this reply is genuinely its answer, not a duplicate.
                return _REPLAY_OK, line, "forwarded_first_answer"
            if msg.get("result") != session.init_result:
                # Fail CLOSED, and terminally. A daemon generation whose
                # configuration moved on can answer with a different server than
                # the one whose tools this session froze at session/new; carrying
                # on would turn a recoverable outage into a session that answers
                # wrongly, which is strictly worse than the terminal exit. It is
                # also not worth retrying: that generation owns the endpoint.
                return _REPLAY_REFUSE, None, "handshake_result_changed"
            return _REPLAY_OK, None, "result_matched"

    try:
        return await asyncio.wait_for(_await_reply(), timeout=_REPLAY_INIT_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        return _REPLAY_RETRY, None, "replay_timeout"
    except (
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        OSError,
        ConnectionError,
    ) as exc:
        return _REPLAY_RETRY, None, f"replay_read_failed: {exc}"


#: Outcomes of :func:`_ensure_backend_admitted`.
_ADMIT_READY = "ready"
_ADMIT_REJECTED = "rejected"
_ADMIT_TIMEOUT = "timeout"
_ADMIT_CLOSED = "closed"


def _admission_control_frame(msg: dict) -> bool:
    """A daemon->stub control frame that renews the silence window and is never
    an answer: ``queued`` (position in the spawn gate), ``keepalive`` (transport
    probe) and ``pong`` (a bridge ping's reply, possible during a respawn)."""
    return msg.get("type") in (_SPAWN_QUEUED_TYPE, _BRIDGE_KEEPALIVE_TYPE, _BRIDGE_PONG_TYPE)


async def _ensure_backend_admitted(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    queue_aware: bool,
    total_budget_secs: float,
    silence_secs: float = _SPAWN_QUEUE_SILENCE_SECS,
    now: Callable[[], float] = time.monotonic,
) -> tuple[str, Optional[dict]]:
    """Send ``ensure_backend`` and wait for the daemon's verdict.

    ``queue_aware`` is whether the daemon advertised ``spawn_queue``. When it
    did, the frame carries ``wait_budget_secs`` and the wait is a SILENCE timer:
    every ``queued`` / ``keepalive`` / ``pong`` frame restarts ``silence_secs``,
    and the whole wait is capped by ``total_budget_secs``. When it did not, the
    daemon answers exactly once and the wait is the legacy single
    ``silence_secs`` read -- an old daemon never sends ``queued`` and must not be
    waited on any longer than before.

    Returns ``(outcome, frame)``: ``ready`` / ``rejected`` with the frame,
    ``timeout`` (silence or budget spent) or ``closed`` (the daemon hung up or
    the transport failed) with ``None``. Never raises for transport reasons; the
    caller decides between fallback, retry and the terminal exit.
    """
    frame: dict = {"type": "ensure_backend"}
    if queue_aware:
        frame["wait_budget_secs"] = total_budget_secs
    try:
        await _write_frame(writer, frame)
    except (OSError, ConnectionError):
        return _ADMIT_CLOSED, None
    deadline = now() + (total_budget_secs if queue_aware else silence_secs)
    while True:
        remaining = deadline - now()
        if remaining <= 0:
            return _ADMIT_TIMEOUT, None
        try:
            msg = await asyncio.wait_for(_read_frame(reader), timeout=min(silence_secs, remaining))
        except asyncio.TimeoutError:
            return _ADMIT_TIMEOUT, None
        except _NonObjectFrame:
            # A stray value costs that frame; the daemon still holds our place.
            continue
        except (OSError, ConnectionError, json.JSONDecodeError):
            return _ADMIT_CLOSED, None
        if msg is None:
            return _ADMIT_CLOSED, None
        if queue_aware and _admission_control_frame(msg):
            # Proof the daemon is alive and still holds our place: renew the
            # silence window, never the total budget.
            continue
        mtype = msg.get("type")
        if mtype == "ready":
            return _ADMIT_READY, msg
        if mtype == "rejected":
            return _ADMIT_REJECTED, msg
        if not queue_aware:
            # Legacy contract: the one reply is the verdict, whatever it is.
            return _ADMIT_REJECTED, msg
        # An unknown control frame from a newer daemon: ignore, keep waiting.


def _rejection_class(frame: dict) -> Optional[str]:
    """The ``class`` a ``rejected`` frame carries, or ``None`` from an old daemon."""
    cls = frame.get("class")
    return cls if isinstance(cls, str) and cls else None


async def _serve_capacity_refusal(
    session: StubSession,
    stop_event: asyncio.Event,
    *,
    reason: str,
    retry_after_secs: Optional[int],
    pool_label: str,
) -> int:
    """Answer kiro-cli's requests with a typed error until it hangs up.

    The daemon refused this connection for ``capacity`` after the full wait
    budget. A per-session exec would put one more process on the host that just
    refused one, so there is no fallback; and dying would make kiro-cli report
    the server as crashed. Instead the stdio transport stays open and every
    request gets a JSON-RPC error carrying the class and the daemon's
    ``retry_after_secs``, so the session -- and the recovery ladder above it --
    can tell "the gateway is full" from "the server is broken". Notifications
    are dropped (they have no id to answer). Returns the process exit code.
    """
    message = f"MCP gateway at capacity for {pool_label}: {reason}"
    while not stop_event.is_set():
        try:
            line = await session.next_line()
        except Exception:  # pragma: no cover -- stdin reader died
            return 1
        if not line:
            return 0
        msg = parse_json_object_line(line)
        if msg is None or "method" not in msg or "id" not in msg:
            continue
        error = {
            "jsonrpc": "2.0",
            "id": msg["id"],
            "error": {
                "code": _CAPACITY_ERROR_CODE,
                "message": message,
                "data": {"class": _REJECT_CLASS_CAPACITY, "retry_after_secs": retry_after_secs},
            },
        }
        payload = (json.dumps(error, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_write_stdout_line, payload),
                timeout=_ERROR_EMIT_TIMEOUT_SECS,
            )
        except (asyncio.TimeoutError, OSError, ValueError):
            return 1
    return 0


def _split_abandoned_ids(session: StubSession) -> tuple[list, Any]:
    """Which in-flight ids to fail now, and which single one to defer.

    An id answered twice is a protocol violation, not a belt-and-braces retry,
    so every id gets exactly one response. Calls are failed retryably because the
    stub cannot know whether the dead daemon forwarded them upstream before it
    died, and replaying them could double a side effect.

    An unanswered ``initialize`` is the one exception. The replay on a fresh
    connection can still answer it for real, so erroring it here would either
    contradict that success or duplicate the error the terminal path emits. It is
    deferred and resolved once -- by the replay if that works, by the terminal
    path if it does not. An ``initialize`` this session ALREADY has a result for
    is not deferred: the replay swallows that reply as a duplicate, so nobody
    downstream would ever answer the id.
    """
    deferred: Any = None
    if session.captured_init is not None and session.init_result is None:
        init_id = session.captured_init_id()
        if init_id is not None and init_id in session.outstanding_ids:
            deferred = init_id
    return [i for i in session.outstanding_ids if i != deferred], deferred


def _reconnect_now() -> float:
    """Monotonic clock the reconnect budget is measured against.

    A module-level seam rather than ``loop.time()`` inline so a test can drive
    ten simulated minutes of budget without sleeping through them.
    """
    return time.monotonic()


async def _reconnect_wait(stop_event: asyncio.Event, delay: float) -> bool:
    """Burn one backoff step. False means a stop arrived; give up.

    Same seam as :func:`_reconnect_now`: the test that proves the budget is
    actually spent replaces this with a fake that advances the injected clock.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
        return False
    except asyncio.TimeoutError:
        return True


def _is_failable_request(msg: Any) -> bool:
    """Is this a frame the drain may answer with an error of its own?

    A JSON-RPC request has both ``method`` and ``id``. A notification has no
    ``id`` and answering one is a protocol violation, so it can only be held.
    ``initialize`` is excluded for the opposite reason: the handshake replay can
    still answer it for real, which is the same exception
    :func:`_split_abandoned_ids` makes for an in-flight one.
    """
    if not isinstance(msg, dict) or "method" not in msg or "id" not in msg:
        return False
    return msg.get("method") != "initialize"


#: What a call queued during a reconnect is told when its grace runs out. One
#: constant because two paths emit it -- the expiry sweep and the retention
#: bound -- and a caller that learns to retry on one must see the same words on
#: the other.
_QUEUED_FAIL_MESSAGE = (
    "Gateway restarted and is not back yet; this call was failed so it would " "not hang. Retry it."
)


def _no_room_to_hold(line: bytes, held: list, held_bytes: int) -> bool:
    """Would retaining ``line`` breach either dimension of the hold bound?

    Both are checked because either alone is a hole: a count bound admits
    ``_STDIN_QUEUE_MAXSIZE`` frames of ``_HELD_FRAME_BYTES`` each, and a byte
    bound leaves the per-object overhead of very many tiny frames unaccounted.
    The per-frame ceiling is checked too, so one enormous line is answered
    immediately rather than parked for the whole reconnect.
    """
    return (
        len(held) >= _STDIN_QUEUE_MAXSIZE
        or len(line) > _HELD_FRAME_BYTES
        or held_bytes + len(line) > _HELD_TOTAL_BYTES
    )


async def _drain_while_disconnected(
    session: StubSession,
    *,
    pool_label: str,
    grace_secs: float,
    stop: asyncio.Event,
) -> None:
    """Answer requests that arrive while the bridge is down, once grace expires.

    Runs CONCURRENTLY with :func:`_reconnect` and is the only consumer of
    kiro-cli's stream for that window, because ``run_bridge`` -- and with it the
    ``stdin_pump`` that normally reads the session queue -- has already returned.
    Without this, a request issued in that window sits in the queue unread for as
    long as the reconnect runs: not forwarded, not failed, and indistinguishable
    from a hang. That is the whole defect; the in-flight half is already answered
    by ``_split_abandoned_ids`` + :func:`_emit_error_frames` before the reconnect
    starts.

    Each frame is HELD first and failed only if ``grace_secs`` passes with the
    bridge still down, so the common case -- an ordinary restart that re-attaches
    in a few seconds -- forwards the call and answers it for real. Nothing is
    forwarded from here: held frames go back to the session
    (:meth:`StubSession.push_front`) and the next bridge sends them in order.
    A frame that WAS failed is dropped instead, because forwarding an id after
    answering it would let the new daemon answer it a second time.

    Three kinds of frame are never failed, only held: a notification (no id to
    answer), an unparseable line (it is forwarded verbatim, as ``stdin_pump``
    does), and ``initialize`` (the replay answers it for real). ``grace_secs`` is
    a parameter purely so a test need not wait the real window.

    Holding is bounded in BOTH dimensions -- ``_STDIN_QUEUE_MAXSIZE`` frames and
    ``_HELD_TOTAL_BYTES`` of them, with ``_HELD_FRAME_BYTES`` per frame -- because
    either bound alone leaves a hole (see :func:`_no_room_to_hold`). At a bound the
    frame is not held, and what happens then depends on whether it can be
    answered: a request is failed immediately, and a frame with no id is dropped
    and logged. Holding is only the courtesy that lets a quick reattach serve a
    call for real, so it is what yields under pressure -- never the answer.

    The ending is COOPERATIVE -- the caller sets ``stop`` and waits -- and that is
    load-bearing rather than tidy. This loop removes a frame from ``held`` when it
    answers it, so a cancellation landing inside the answering ``await`` would
    leave that frame neither forwarded nor reliably answered: a silent hang, the
    one outcome this function exists to remove. A stop is only ever observed
    BETWEEN iterations, so an answer in flight always completes. Everything still
    held is handed back in the ``finally``.
    """
    # (line, id-to-fail-or-None, deadline). One ordered list rather than a queue
    # plus a timer set, so removing an expired request cannot reorder the frames
    # that outlive it.
    held: list[tuple[bytes, Any, float]] = []
    held_bytes = 0
    # Both waitables are typed as the same Future[Any] so one ``asyncio.wait``
    # can hold them together; the loop tells them apart by identity, not by type.
    pending: Optional["asyncio.Future[Any]"] = None
    stop_wait: Optional["asyncio.Future[Any]"] = None
    try:
        while True:
            # Answer whatever is due FIRST, on every pass. Doing this only when
            # the wait below times out would let a caller that keeps the queue
            # ready starve the sweep: each pass would take a new line and loop,
            # and the grace -- the one promise this function makes -- would never
            # be enforced on the lines already held.
            now = _reconnect_now()
            expired = [rid for _l, rid, d in held if rid is not None and d <= now]
            if expired:
                kept = [entry for entry in held if entry[1] is None or entry[2] > now]
                held_bytes = sum(len(line) for line, _rid, _d in kept)
                held = kept
                await _emit_error_frames(expired, _QUEUED_FAIL_MESSAGE, pool_label=pool_label)
                logger.info(
                    "stub reconnect: failed %d call(s) queued while disconnected " "pool=%s",
                    len(expired),
                    pool_label,
                )
            if pending is None:
                # Kept ACROSS timeouts rather than re-issued: cancelling a
                # ``Queue.get`` that has already been woken is the one way this
                # loop could drop a line kiro-cli sent.
                pending = asyncio.ensure_future(session.next_line())
            deadlines = [d for _l, rid, d in held if rid is not None]
            timeout = max(0.0, min(deadlines) - _reconnect_now()) if deadlines else None
            if stop_wait is None:
                stop_wait = asyncio.ensure_future(stop.wait())
            done, _still = await asyncio.wait(
                {pending, stop_wait},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_wait in done:
                return
            if pending in done:
                line = pending.result()
                pending = None
                if not line:
                    # kiro-cli closed stdin. Hold the EOF so the next
                    # ``stdin_pump`` reports ``stdin_eof`` exactly as it would
                    # have, and stop draining: nothing else is coming.
                    held.append((line, None, 0.0))
                    return
                # Any: ``failable`` below is what proves it a dict with an id.
                msg: Any = parse_json_object_line(line)
                failable = _is_failable_request(msg)
                if _no_room_to_hold(line, held, held_bytes):
                    # Retention is full in one of its dimensions, so this frame
                    # cannot be kept for the new bridge. What it can still be is
                    # ANSWERED: holding exists only to give a quick reattach the
                    # chance to serve the call for real, and that courtesy is what
                    # yields at a bound -- never the answer itself, which is the
                    # one outcome a caller cannot work around.
                    if failable:
                        await _emit_error_frames(
                            [msg["id"]], _QUEUED_FAIL_MESSAGE, pool_label=pool_label
                        )
                        continue
                    # Nothing here to answer: a notification has no id, and
                    # neither has a line that does not parse. Dropped rather than
                    # held, for the same reason ``_serve_capacity_refusal`` drops
                    # them, and logged because it is a real loss.
                    logger.warning(
                        "stub reconnect: dropped an unanswerable frame at the "
                        "retention bound, held=%d/%d bytes=%d/%d pool=%s",
                        len(held),
                        _STDIN_QUEUE_MAXSIZE,
                        held_bytes,
                        _HELD_TOTAL_BYTES,
                        pool_label,
                    )
                    continue
                held_bytes += len(line)
                if failable:
                    held.append((line, msg["id"], _reconnect_now() + grace_secs))
                else:
                    held.append((line, None, 0.0))
    finally:
        if stop_wait is not None and not stop_wait.done():
            stop_wait.cancel()
        if pending is not None:
            if pending.done() and not pending.cancelled():
                with contextlib.suppress(Exception):
                    held.append((pending.result(), None, 0.0))
            else:
                pending.cancel()
        session.push_front([line for line, _rid, _d in held])


async def _reconnect(
    socket_path: str,
    payload: dict,
    session: StubSession,
    stop_event: asyncio.Event,
    *,
    poolable: bool,
    pool_label: str,
) -> Optional[tuple[asyncio.StreamReader, asyncio.StreamWriter, dict, str]]:
    """Re-attach to a restarted gateway, or give up within a finite budget.

    Retries the Register handshake because the daemon's supervisor respawns it
    with its own backoff, so the endpoint is typically absent for a moment
    rather than gone -- and retries a handshake replay that lost its connection
    for the same reason, since the daemon reached first may still be starting or
    may die again. What is NOT retried is a refusal: an older daemon will not
    grow the ``poolable_ack`` capability, and a generation that answers the
    handshake differently will keep answering that way, so retrying either only
    delays the terminal exit.

    While this runs the stdio transport to kiro-cli stays open: the stdin reader
    thread keeps queueing whatever kiro-cli sends (bounded, then backpressured
    onto the pipe) and the next ``run_bridge`` forwards it to the new
    connection. Nothing is dropped and nothing is closed until the budget is
    genuinely spent -- see the ``_RECONNECT_TOTAL_BUDGET_SECS`` comment for why
    that budget is minutes, not seconds.
    """
    started = _reconnect_now()
    deadline = started + _RECONNECT_TOTAL_BUDGET_SECS
    delay = _RECONNECT_BACKOFF_START_SECS

    async def _pause() -> bool:
        """Burn one backoff step. False means a stop arrived; give up."""
        nonlocal delay
        if not await _reconnect_wait(stop_event, delay):
            return False
        delay = min(delay * 2, _RECONNECT_BACKOFF_MAX_SECS)
        return True

    while not stop_event.is_set() and _reconnect_now() < deadline:
        # A fresh handle per ATTEMPT, not merely per reconnect. The daemon keys
        # attach, refcount and the private-backend map on this uuid, and an
        # attempt that died mid-replay may leave its registration in place for a
        # moment -- so retrying under the same uuid would let the predecessor's
        # teardown remove the replacement's backend, which is the very collision
        # a per-registration handle exists to avoid. Minted here rather than by
        # the caller because only this loop knows how many attempts there were.
        payload = {**payload, "stub_uuid": str(uuid.uuid4())}
        try:
            reader, writer, _uuid, registered = await asyncio.wait_for(
                handshake(socket_path, payload), timeout=_HANDSHAKE_TIMEOUT_SECS
            )
        except StaleGenerationError as exc:
            # Caught BEFORE the transient arm below, which it would otherwise
            # match as a plain FallbackRequestedError. The daemon that answered
            # is up and of another code generation -- the endpoint a reconnect
            # binds to need not be the one this stub first registered with --
            # and it will answer every later attempt the same way, so this is
            # the refusal the docstring names, not an outage. Cold start
            # degrades to a per-session exec; here ``initialize`` is long
            # consumed, so the honest move is the terminal exit, exactly as for
            # the two capability refusals below. ``handshake`` already closed
            # the connection.
            logger.warning(
                "stub reconnect: handshake refused the new gateway generation "
                "(%s); terminal, not retried -- a daemon of another generation "
                "keeps answering that way pool=%s",
                exc.reason,
                pool_label,
            )
            return None
        except (asyncio.TimeoutError, FallbackRequestedError) as exc:
            now = _reconnect_now()
            logger.info(
                "stub reconnect: gateway not back yet (%s) elapsed=%.0fs "
                "remaining=%.0fs next_retry_in=%.1fs pool=%s",
                exc,
                now - started,
                max(0.0, deadline - now),
                delay,
                pool_label,
            )
            if not await _pause():
                return None
            continue

        _caps = registered.get("capabilities") if isinstance(registered, dict) else None
        _caps = _caps if isinstance(_caps, list) else []
        # The same check the cold-start handshake makes, for the same reason and
        # with more force here: the endpoint a reconnect binds to need not be
        # the generation this stub first registered with, and the manager adopts
        # anything answering ``pong`` with no version handshake. A daemon
        # predating the ``poolable`` field ignores it and routes this register
        # through the shared index, co-tenanting a server the operator never
        # allowlisted. Cold start degrades to a per-session exec; here that is
        # not available (``initialize`` is long consumed), so the honest move is
        # to refuse this generation and take the terminal exit. Refusal is
        # terminal, not retried -- an older daemon will not grow the capability
        # on the next attempt.
        if must_degrade_unshareable(poolable, _caps):
            await _safe_close(writer)
            logger.warning(
                "stub reconnect: the new gateway generation does not honour the "
                "poolable field and this server is not shareable; refusing "
                "rather than risk being pooled pool=%s",
                pool_label,
            )
            return None

        # The mirror case on this path, refused for the reason the check above is
        # refused here rather than degraded: ``initialize`` is long consumed, so
        # no clean per-session exec remains. It belongs on the reconnect path at
        # all because the endpoint a reconnect binds to need not be the
        # generation that answered the first register -- a nonce-capable daemon
        # can be replaced by an adopted one between two attempts, and pooling on
        # without a nonce would put every unnamed co-tenant of this server in one
        # namespace.
        if must_degrade_nonce_blind(str(payload.get("server_name") or ""), poolable, _caps):
            await _safe_close(writer)
            logger.warning(
                "stub reconnect: the new gateway generation mints no "
                "per-connection tenant nonce and this server separates unnamed "
                "co-tenants by it; refusing rather than pooling without it "
                "pool=%s",
                pool_label,
            )
            return None

        if "ensure_backend" in _caps:
            # Admission BEFORE the replay, as at cold start. Without it the
            # replayed ``initialize`` is the daemon's first frame from this
            # connection and lands on its lazy-spawn path, where a full daemon
            # answers with a terminal rejection after a silent 20 s wait. Through
            # the pre-flight a queue-aware daemon holds this stub in its spawn
            # gate with ``queued`` keepalives for as long as the remaining
            # reconnect budget allows, and says ``capacity`` when it cannot --
            # which is retried here, because a daemon that is full now is the
            # likeliest shape of the restart this loop exists to outlast.
            outcome, verdict = await _ensure_backend_admitted(
                reader,
                writer,
                queue_aware="spawn_queue" in _caps,
                total_budget_secs=max(1.0, deadline - _reconnect_now()),
                now=_reconnect_now,
            )
            if outcome != _ADMIT_READY:
                await _safe_close(writer)
                cls = _rejection_class(verdict) if verdict is not None else None
                refuse = outcome == _ADMIT_REJECTED and (
                    cls in _FALLBACK_CLASSES
                    or (cls is None and not bool((verdict or {}).get("fallback")))
                )
                if refuse:
                    # ``compat`` / ``isolation`` (or an old daemon's terminal
                    # rejection): the target, not the moment. ``initialize`` is
                    # long consumed, so the cold-start exec is not available;
                    # refuse this generation.
                    logger.warning(
                        "stub reconnect: gateway rejected ensure_backend (%s: %s); "
                        "refusing pool=%s",
                        cls,
                        (verdict or {}).get("reason"),
                        pool_label,
                    )
                    return None
                logger.info(
                    "stub reconnect: not admitted yet (%s%s); retrying pool=%s",
                    outcome,
                    f": {(verdict or {}).get('reason')}" if verdict else "",
                    pool_label,
                )
                if not await _pause():
                    return None
                continue

        status, forward, detail = await _replay_initialize(reader, writer, session)
        if status == _REPLAY_RETRY:
            # The connection died mid-replay, which says nothing about what the
            # answer would have been -- and this is the likeliest shape of a
            # supervisor respawn, where the daemon reached first may still be
            # starting or may die again. Spend a backoff step and try a fresh
            # connection rather than burning the session here.
            await _safe_close(writer)
            logger.info(
                "stub reconnect: lost the connection during handshake replay "
                "(%s); retrying pool=%s",
                detail,
                pool_label,
            )
            if not await _pause():
                return None
            continue
        if status != _REPLAY_OK:
            await _safe_close(writer)
            logger.warning(
                "stub reconnect: refusing the new gateway generation (%s) pool=%s",
                detail,
                pool_label,
            )
            return None
        if forward is not None:
            # The session's original handshake never got an answer; hand this one
            # to kiro-cli rather than swallowing it.
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(_write_stdout_line, forward),
                    timeout=_ERROR_EMIT_TIMEOUT_SECS,
                )
            except (asyncio.TimeoutError, OSError, ValueError):
                await _safe_close(writer)
                logger.warning(
                    "stub reconnect: could not deliver the replayed handshake " "pool=%s",
                    pool_label,
                )
                return None
            session.note_init_result(json.loads(forward))
        session.reconnects += 1
        from kiro_crew.metrics.events import MCP_RECONNECTS, emit_counter

        emit_counter(MCP_RECONNECTS, {"pool": bool(pool_label)})
        logger.info(
            "stub reconnected to a restarted gateway (%s, reconnect #%d) pool=%s",
            detail,
            session.reconnects,
            pool_label,
        )
        return (
            reader,
            writer,
            registered if isinstance(registered, dict) else {},
            payload["stub_uuid"],
        )
    return None


def _write_stdout_line(line: bytes) -> None:
    """Write one whole line to kiro-cli's stdin, flushed."""
    sys.stdout.buffer.write(line if line.endswith(b"\n") else line + b"\n")
    sys.stdout.buffer.flush()


async def _reconnect_while_draining(
    socket_path: str,
    payload: dict,
    session: StubSession,
    stop_event: asyncio.Event,
    *,
    poolable: bool,
    pool_label: str,
) -> Optional[tuple[asyncio.StreamReader, asyncio.StreamWriter, dict, str]]:
    """:func:`_reconnect`, with kiro-cli's stream still being read while it runs.

    The two are paired in one function rather than inline in the serve loop so the
    PAIRING is testable. Neither half is meaningful alone: a reconnect running
    without the drain leaves requests that arrive meanwhile unread for the whole
    budget, and a drain that exists but is never started alongside the reconnect
    does nothing at all. This shape is what makes that pairing assertable.

    The drain is the only reader of the session's queue for this window, and it is
    stopped -- and AWAITED -- before returning, so the bridge that follows finds
    exactly one consumer and a stream still in kiro-cli's order.
    """
    stop_draining = asyncio.Event()
    drain = asyncio.ensure_future(
        _drain_while_disconnected(
            session,
            pool_label=pool_label,
            grace_secs=_QUEUED_GRACE_SECS,
            stop=stop_draining,
        )
    )
    try:
        return await _reconnect(
            socket_path,
            payload,
            session,
            stop_event,
            poolable=poolable,
            pool_label=pool_label,
        )
    finally:
        # ASKED to stop, not cancelled: the drain removes a frame from its hold
        # when it answers it, so a cancellation landing inside that answer would
        # lose the frame both ways. It observes the stop between iterations, so an
        # answer in flight finishes first. The wait is bounded because the only
        # thing that can hold it is the same wedged-stdout case
        # ``_emit_error_frames`` already bounds; a cancel is the last resort there,
        # and by then the caller cannot be answered at all.
        stop_draining.set()
        try:
            await asyncio.wait_for(asyncio.shield(drain), timeout=_ERROR_EMIT_TIMEOUT_SECS + 1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain
        # Either way the drain has run its ``finally`` and handed back everything
        # it held before the next pump reads a line -- two readers on one queue is
        # what would reorder the stream.
