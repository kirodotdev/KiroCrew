"""KiroCrew MCP stub — shim between kiro-cli and the gateway daemon.

The stub is the shim kiro-cli execs in place of the real MCP binary. It
connects to gatewayd over a unix socket, Registers with a full
:class:`PoolKey` payload, then bridges kiro-cli stdio ↔ gateway until
either side closes. On handshake failure it logs a structured fallback
record to ``$KIROCREW_HOME/logs/stub_fallback.jsonl`` and hands the session
to the real MCP backend, preserving per-session correctness: ``execvpe`` in
place on POSIX, and on Windows -- which has no in-place exec -- a child
inheriting this process's stdio (see :func:`_fallback_spawn_child`).

Register fields match :meth:`PoolKey.from_register`; hashes use SHA-256
(stdlib). Bridge phase is NOT wrapped in a timeout (learned correction
— a single timeout around a long-lived session silently kills healthy
streams). Import budget: stdlib + pool + mcp_caller only.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, NoReturn, Optional

from kiro_crew import platform_compat
from kiro_crew.executors import configure_default_executor, subprocess_executor
from kiro_crew.jsonl_util import bounded_records, rotate_jsonl_at
from kiro_crew.mcp_caller import (
    POOLING_REQUIRES_TENANT_NONCE,
    CallerContext,
    _parent_pid,
)
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.hashing import (
    decode_target_args,
    expand_stub_flags,
    hash_command,
    hash_effective_env,
)
from kiro_crew.mcp_gateway.pool import (
    _DEFAULT_READ_BUFFER_LIMIT,
    READ_BUFFER_LIMIT_BYTES,
    PoolKey,
)
from kiro_crew.mcp_gateway.shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS
from kiro_crew.metrics.events import MCP_RECONNECTS, emit_counter

logger = logging.getLogger(__name__)

_HANDSHAKE_TIMEOUT_SECS = 3.0

# --- Bridge liveness (ping-while-outstanding) constants ---------------------
# Interval between successive stub→gateway liveness pings while at least one
# JSON-RPC request is outstanding. A peer that is merely slow (but alive)
# responds to pings even while processing a long tool call.
_BRIDGE_PING_INTERVAL_SECS = 10.0
# How many CONSECUTIVE pings must go unanswered before declaring the peer dead.
# Total grace period is _BRIDGE_PING_INTERVAL_SECS × _BRIDGE_PING_MAX_MISSES.
_BRIDGE_PING_MAX_MISSES = 3
# Reserved type field for the stub→gateway liveness ping control frame and its
# response. The gateway echoes {"type": "pong"} for any {"type": "ping"} it
# receives from a registered stub.
_BRIDGE_PING_TYPE = "ping"
# Cap on emitting the liveness error frames. Bounded because the whole point of
# that path is to stop a caller hanging — an unbounded write to a wedged reader
# would reproduce the defect it exists to fix.
_ERROR_EMIT_TIMEOUT_SECS = 5.0
_BRIDGE_PONG_TYPE = "pong"
# Reserved type for the gateway->stub keepalive control frame. The gateway
# writes one to every live stub each heartbeat sweep so that a half-open
# transport — which a parked reader cannot observe — fails an actual write and
# becomes detectable. It carries no payload and expects no reply: the write
# succeeding or failing IS the signal, and it is consumed here rather than
# forwarded, exactly like the pong frame above.
_BRIDGE_KEEPALIVE_TYPE = "keepalive"
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
# this file shows a slow daemon.
#
# Short enough to matter: an ordinary restart re-attaches inside it and the call
# is served for real, so the error path is reached only by the crash loop the
# budget exists for. Long enough to matter: it is not zero, because failing a
# call the next second would have answered turns a recoverable hiccup into a
# visible tool error for no gain.
_QUEUED_GRACE_SECS = _BRIDGE_PING_INTERVAL_SECS * _BRIDGE_PING_MAX_MISSES
# Lines the stdin reader thread may hold for a consumer that is not reading. The
# reader BLOCKS on a full queue, so kiro-cli's pipe applies backpressure and a
# slow consumer cannot balloon RSS. ``_drain_while_disconnected`` holds itself to
# the same COUNT for the same reason: it moves lines out of the queue, so a bound
# of its own is what keeps that guarantee from being defeated.
_STDIN_QUEUE_MAXSIZE = 256
# Byte bounds on what ``_drain_while_disconnected`` RETAINS. Both dimensions are
# load-bearing, the same pair and the same reasoning as
# ``gatewayd._MAX_PENDING_FRAMES`` / ``_MAX_PENDING_BYTES``: a count bound alone
# admits ``_STDIN_QUEUE_MAXSIZE`` x the largest line the transport will carry, and
# a byte bound alone leaves the per-object overhead of very many tiny frames
# unaccounted. The aggregate follows the per-frame ceiling upward so one frame the
# reader was willing to return can never trip it alone, and is floored at the
# SHIPPED read limit so tuning ``mcp_gateway.read_buffer_limit_bytes`` down (1 KiB
# is accepted) cannot tighten the hold along with it.
_HELD_FRAME_BYTES = READ_BUFFER_LIMIT_BYTES
_HELD_TOTAL_BYTES = max(_DEFAULT_READ_BUFFER_LIMIT, _HELD_FRAME_BYTES)
# Coupled to the DEFAULT of ``mcp_gateway.spawn_queue_wait_secs``
# (``config/sections.py``): the daemon holds a queued stub for about that long
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
# Daemon->stub control frame while queued behind the spawn gate. Consumed by
# the pre-flight, the reconnect replay and the bridge; never forwarded.
_SPAWN_QUEUED_TYPE = "queued"
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
# Content-hash cap for ``binary_version``. Every shipped MCP is <1 MiB, so
# 4 MiB covers them with margin. Larger binaries
# (npm/Node/Java runtimes) are NOT hashed synchronously on the cold-start
# path — a 64 MiB hash blocked the stub event loop ~150-300 ms — they fall
# back to a cheap (size, mtime) token, which is still a stable pool-split key.
_BINARY_HASH_CAP_BYTES = 4 * 1024 * 1024
# Placeholder: stub does not yet observe a config snapshot, so all
# same-session stubs agree on this value (never a false split).
# Safety note: approval_mode and sandbox_mode are already separate PoolKey
# dimensions, so the dangerous config divergences (permission escalation,
# sandbox escape) are already covered by distinct pool entries.
# TODO: Hash relevant config fields (e.g. tool allowlists,
# hook settings) in a future iteration to detect non-security config drift
# that could cause subtle behavioral differences across pooled sessions.
_CONFIG_SNAPSHOT_PLACEHOLDER = "0" * 64


def _crew_home() -> Path:
    """Data home the stub resolves its paths under.

    ``Path.home()`` rather than ``os.environ["HOME"]``: that variable is
    normally unset on Windows (which uses ``USERPROFILE``), so the previous
    fallback evaluated to ``Path("")`` and every derived path became RELATIVE to
    the stub's cwd. For the socket that is worse than untidy on Windows, where
    the pipe name is a hash of this path -- a daemon and a stub started from
    different working directories would hash to different pipe names and never
    meet. ``Path.home()`` consults the right variable on each platform.

    Never raises. ``Path.home()`` raises ``RuntimeError`` when no home can be
    resolved at all, and both callers are on paths that must not fail: the
    socket default is an argparse default (a raise there kills the stub before
    it can degrade to a per-session exec) and the log path is used by
    ``log_fallback``, whose handler catches ``OSError`` only.

    Deliberately not ``config.paths.config_dir()``: this module is on the stub's
    cold-start path and stays import-light.
    """
    home = os.environ.get("KIROCREW_HOME")
    if home:
        return Path(home)
    try:
        base = Path.home()
    except RuntimeError:
        base = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".")
    return base / ".kiro" / "crew"


def _default_socket_path() -> str:
    """Resolve the default gateway socket under KIROCREW_HOME (0700 dir)."""
    home = _crew_home()
    new_path = home / "kirocrew-mcp-gateway.sock"
    # Accept legacy socket name written by older versions.
    legacy_path = home / "mc-mcp-gateway.sock"
    if not new_path.exists() and legacy_path.exists():
        return str(legacy_path)
    return str(new_path)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse the argv shape produced by
    :func:`kiro_crew.mcp_gateway.rewriter.rewrite_agents`. ``--real-stub``
    is accepted and ignored — older installations' overlay wrappers may
    still pass it; we swallow the flag so the stub stays backward-
    compatible with on-disk agent overlays written by earlier rewriter
    revisions.

    The rewriter emits the flags as one ``--stub-flags-b64`` envelope so raw
    paths and identifiers cross a cmd.exe launch without ``%NAME%`` expansion;
    it is spliced back into plain tokens here, ahead of the parser, and an
    overlay that spells the flags out directly parses the same way.
    """
    argv = expand_stub_flags(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(
        prog="kirocrew-mcp-stub",
        description="KiroCrew MCP shim: proxies kiro-cli stdio to the local gateway",
    )
    p.add_argument("--server", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--target-command", required=True, dest="target_command")
    p.add_argument(
        "--target-args-b64",
        default=None,
        help="Base64url-encoded JSON argv; takes precedence over --target-args.",
    )
    p.add_argument("--target-args", default="", help="Legacy delimiter-separated argv.")
    p.add_argument("--target-args-sep", default="|", dest="target_args_sep")
    p.add_argument("--sandbox-mode", default="standard", dest="sandbox_mode")
    p.add_argument("--work-dir", required=True, dest="work_dir")
    p.add_argument("--env", default="", help="Legacy CSV env (DEPRECATED; use --env-json)")
    p.add_argument(
        "--env-json",
        default="",
        dest="env_json",
        help="JSON-encoded env pairs. Preferred over --env because values may "
             "contain ',' or '=' which CSV silently truncates.",
    )
    p.add_argument(
        "--env-file",
        default="",
        dest="env_file",
        help="Path to a 0600 JSON file of env pairs. Preferred over "
             "--env-json so secrets never appear on argv "
             "(/proc/<pid>/cmdline).",
    )
    p.add_argument("--auto-approve", default="", dest="auto_approve")
    p.add_argument("--approval-mode", default="interactive", dest="approval_mode")
    p.add_argument("--trust-all", action="store_true", dest="trust_all")
    p.add_argument(
        "--poolable",
        action="store_true",
        dest="poolable",
        help=(
            "Declare this connection's backend shareable with other connections "
            "carrying an identical PoolKey. Absent, the gateway gives this "
            "connection its own backend -- the same process topology as no "
            "gateway at all, which is why absent is the safe default: a stub "
            "from an overlay predating this flag never silently starts sharing."
        ),
    )
    p.add_argument("--channel-id", default=None, dest="channel_id")
    p.add_argument(
        "--pool-identity-env-b64",
        default=None,
        help="Base64url-encoded JSON env names; takes precedence over --pool-identity-env.",
    )
    p.add_argument(
        "--pool-identity-env",
        default="",
        dest="pool_identity_env",
        help=(
            "DEPRECATED: env variable NAMES separated by --target-args-sep. "
            "Read only when --pool-identity-env-b64 is absent. Folded into "
            "effective_env_hash even when the name looks like a rotating "
            "secret. Names only, never values, so this is safe on argv. "
            "Carries NO authority: gatewayd re-reads the operator's own list "
            "at spawn and refuses to forward when the hash it recomputes does "
            "not match the one registered here, so a stub cannot widen what "
            "gets applied to a shared backend."
        ),
    )
    p.add_argument(
        "--socket",
        default=os.environ.get("KIROCREW_MCP_SOCKET") or os.environ.get("MC_MCP_SOCKET") or _default_socket_path(),
    )
    p.add_argument("--real-stub", default=None, dest="real_stub")
    return p.parse_args(argv)


def _split_target_args(raw: str, sep: str) -> list[str]:
    """Read delimiter-separated arguments from legacy overlays."""
    return raw.split(sep) if raw else []


def _resolve_target_args(args: argparse.Namespace) -> list[str]:
    """Share one decode between backend launch and the registered command hash."""
    encoded = getattr(args, "target_args_b64", None)
    if encoded is not None:
        return decode_target_args(encoded)
    return _split_target_args(args.target_args, args.target_args_sep)


def _resolve_pool_identity_env(args: argparse.Namespace) -> frozenset[str]:
    """Decode names only; the daemon independently checks forwarding authority."""
    encoded = getattr(args, "pool_identity_env_b64", None)
    names = (
        decode_target_args(encoded)
        if encoded is not None
        else _split_target_args(args.pool_identity_env, args.target_args_sep)
    )
    return frozenset(n for n in names if n)


def _parse_env_csv(raw: str) -> dict[str, str]:
    """Parse ``K=V,K2=V2``; malformed fragments are skipped.

    DEPRECATED: values containing ``,`` get truncated. New callers should
    use ``--env-json`` which round-trips cleanly through
    :func:`_parse_env_json`.
    """
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k:
            out[k] = v
    return out


def _parse_env_json(raw: str) -> dict[str, str]:
    """Parse a JSON-encoded env dict. Returns ``{}`` on empty / malformed.

    Preferred over :func:`_parse_env_csv` because values with ``,`` or
    ``=`` round-trip intact. Non-string values are coerced via ``str()``.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("stub --env-json malformed; dropping env block")
        return {}
    if not isinstance(decoded, dict):
        logger.warning("stub --env-json not a JSON object; dropping env block")
        return {}
    return {str(k): str(v) for k, v in decoded.items() if k}


def _parse_env_file(path: str) -> dict[str, str]:
    """Read a JSON env dict from ``path`` (a 0600 sidecar). Returns ``{}``
    on missing/malformed. Keeps env secrets off argv; same coercion as
    :func:`_parse_env_json`.
    """
    if not path:
        return {}
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        logger.warning("stub --env-file unreadable; dropping env block")
        return {}
    return _parse_env_json(raw)


def _parse_auto_approve(raw: str) -> list[str]:
    if not raw:
        return []
    # New form: JSON array (escape-safe for tool names containing commas).
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(s) for s in parsed if s]
    except ValueError:
        pass
    # Back-compat: legacy CSV form.
    return [s for s in raw.split(",") if s]


def _hash_permission_profile(
    auto_approve: list[str], approval_mode: str, trust_all: bool
) -> str:
    """Hash ``(autoApprove sorted, approval_mode, trust_all)`` — two
    sessions with different permission surfaces MUST NOT share a backend."""
    h = hashlib.sha256()
    for tool in sorted(auto_approve):
        h.update(tool.encode("utf-8"))
        h.update(b"\0")  # NUL delimiter: injective — cannot occur in a tool name,
        #                  so ["a,b"] and ["a","b"] cannot collide onto one key.
    h.update(b"mode=")
    h.update(approval_mode.encode("utf-8"))
    h.update(b"\0trust_all=")
    h.update(b"1" if trust_all else b"0")
    return h.hexdigest()


def _binary_version(command: str) -> str:
    """Return a stable version token for the target binary, or ``"unknown"``.

    Content-hashes binaries up to :data:`_BINARY_HASH_CAP_BYTES`; larger ones
    use a cheap ``(size, mtime)`` token so the stub cold-start path never
    blocks on a multi-MiB synchronous hash.
    """
    try:
        real = os.path.realpath(shutil.which(command) or command)
        if not os.path.isfile(real):
            return "unknown"
        st = os.stat(real)
        if st.st_size > _BINARY_HASH_CAP_BYTES:
            return f"sz{st.st_size}-mt{int(st.st_mtime)}"
        h = hashlib.sha256()
        with open(real, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()[:24]
    except OSError:
        return "unknown"


#: Kiro Crew's own MCP servers, as the subcommand a stub's target args name.
#: For these the target binary is the ``kirocrew`` console-script shim, whose
#: bytes never change across a ``git pull`` of an editable install, so its
#: hash alone let a pooled backend from a two-day-old checkout keep answering
#: stubs from a freshly restarted gateway. The package's own fingerprint is
#: folded in for exactly these, and only these: a third-party MCP binary is
#: what its bytes say it is, and re-partitioning its pool on every Kiro Crew
#: commit would cold-start it for no reason.
_KIROCREW_MCP_SUBCOMMANDS = frozenset(
    {"mcp-core", "mcp-cron", "mcp-work", "mcp-computer", "mcp-dashboard", "mcp-crew-log"}
)


def pool_binary_version(command: str, target_args: list[str]) -> str:
    """The ``binary_version`` a stub registers: the binary's hash, plus the Kiro
    Crew code fingerprint when the target is one of Kiro Crew's own servers.
    """
    base = _binary_version(command)
    if not any(a in _KIROCREW_MCP_SUBCOMMANDS for a in target_args):
        return base
    # Imported here, not at module top: the stub's cold-start path is timed
    # and this module is only needed on the Kiro Crew branch.
    from kiro_crew.code_fingerprint import code_fingerprint

    return f"{base}+{code_fingerprint()}"


def binary_fingerprint(command: str) -> str:
    """Public alias of :func:`_binary_version`.

    The shareability cache keys on the same token the pool does, so an in-place
    binary upgrade invalidates both. One implementation, two consumers — a second
    copy would drift and let one of them keep a stale identity.
    """
    return _binary_version(command)


def _resolve_channel_id(cli_value: Optional[str]) -> Optional[str]:
    """``--channel-id`` wins; else ``KIROCREW_CHANNEL_ID`` env; else None."""
    return cli_value or os.environ.get("KIROCREW_CHANNEL_ID") or None


#: Ancestor-walk depth cap for the Register ``ancestor_pids`` chain. The real
#: tree is ~4 deep (sandbox wrapper → kiro-cli → kiro-cli-chat → stub); 10
#: leaves headroom without letting a pathological /proc loop run away.
_ANCESTOR_WALK_MAX = 10


def _ancestor_pids() -> list[int]:
    """Ancestor PID chain of this stub, nearest parent first.

    Uses :func:`kiro_crew.mcp_caller._parent_pid`, which delegates to
    ``platform_compat.get_ppid``: ``/proc`` on Linux, libproc on macOS,
    ``CreateToolhelp32Snapshot`` on Windows -- none of which spawns a
    subprocess. Stops at PID 1, a
    lookup failure, or the depth cap. Always contains at least
    ``os.getppid()`` when resolvable.
    """
    chain: list[int] = []
    pid = os.getppid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(chain) < _ANCESTOR_WALK_MAX:
        chain.append(pid)
        seen.add(pid)
        pid = _parent_pid(pid)
    return chain


def _build_caller_block(channel_id: Optional[str]) -> dict[str, str]:
    """Assemble caller-identity from ``KIROCREW_*`` env vars.

    ``session_key`` is resolved via :meth:`CallerContext.from_env`, which
    reads ``KIROCREW_SESSION_KEY`` first and falls back to the warm-pool PID
    file (``config_dir()/session_pid_<pid>.txt``) by walking the process
    ancestry. Warm-pool kiro-cli is pre-spawned with NO session key (the key
    is only known once the session is claimed), so a bare env read here would
    register an empty caller and gatewayd would stamp ``caller=None`` on every
    forwarded call — silently breaking state-mutating tools (``learn_add`` et
    al.) that need session identity. Sharing the backend-side resolver keeps
    both ends of the wire in agreement. If the key is still unknown at register
    (claim hasn't happened yet), the recaller loop repairs it later."""
    session_key = CallerContext.from_env().session_key
    # Diagnostic identity only — the OS user. USERNAME is the Windows spelling
    # of USER; check both so this dimension is not empty on one platform.
    principal = (
        os.environ.get("USER") or os.environ.get("USERNAME") or ""
    )
    if session_key.startswith("cron:"):
        session_type = "cron"
    elif session_key.startswith("hook:"):
        session_type = "hook"
    elif session_key.startswith("dashboard:"):
        session_type = "dashboard"
    elif session_key:
        session_type = "slack-thread"
    else:
        session_type = "unknown"
    return {
        "session_key": session_key,
        "session_type": session_type,
        "principal_id": principal,
        "channel_id": channel_id or "",
    }


def build_register_payload(args: argparse.Namespace) -> dict:
    """Compute every PoolKey field and assemble the Register frame.

    The result is accepted verbatim by :meth:`PoolKey.from_register` —
    callers do not post-process."""
    target_args = _resolve_target_args(args)
    # Prefer --env-json when present (commas/equals round-trip intact);
    # fall back to the legacy --env CSV for overlay files written by a
    # pre-JSON rewriter that may still be on disk during the transition.
    if args.env_file:
        env_pairs = _parse_env_file(args.env_file)
    elif args.env_json:
        env_pairs = _parse_env_json(args.env_json)
    else:
        env_pairs = _parse_env_csv(args.env)
    auto_approve = _parse_auto_approve(args.auto_approve)
    channel_id = _resolve_channel_id(args.channel_id)
    identity_keys = _resolve_pool_identity_env(args)

    try:
        work_dir = str(Path(args.work_dir).resolve())
    except OSError:
        work_dir = str(args.work_dir)

    caller = _build_caller_block(channel_id)
    # Per-session token from the injected ACP entry's own env
    # (``session_servers.attach_stub_session_token``). Absent for a stub
    # launched from a hand-written config or an overlay predating the token: the
    # field is then omitted below and gatewayd keeps its PID-keyed behavior.
    session_token = os.environ.get(STUB_SESSION_TOKEN_ENV, "")

    payload = {
        "type": "register",
        "stub_uuid": str(uuid.uuid4()),
        "server_name": args.server,
        "agent_name": args.agent,
        "command_args_hash": hash_command(args.target_command, target_args),
        "effective_env_hash": hash_effective_env(env_pairs, identity_keys=identity_keys),
        "work_dir": work_dir,
        "binary_version": pool_binary_version(args.target_command, target_args),
        # Not os.getuid(): that attribute does not exist on Windows, where an
        # AttributeError here would abort the Register frame and send every
        # session to per-session exec -- pooling would appear enabled and
        # never take effect. local_user_id() is the uid on POSIX and a
        # SID-derived int on Windows, so the PoolKey dimension keeps both its
        # type and its partitioning meaning.
        "os_uid": platform_compat.local_user_id(),
        "sandbox_mode": args.sandbox_mode,
        "autoapprove_set_hash": _hash_permission_profile(
            auto_approve, args.approval_mode, bool(args.trust_all)
        ),
        "approval_mode": args.approval_mode,
        "trust_all_tools": bool(args.trust_all),
        # Not a PoolKey dimension: it selects HOW the backend is acquired
        # (shared bucket vs this connection's own), not WHICH backends are
        # interchangeable. Keeping it out of the key is what lets a
        # per-connection backend exist without making every key
        # connection-private.
        "poolable": bool(args.poolable),
        # Wire-compat ballast, NOT a pool dimension: an adopted daemon that
        # outlived a package upgrade (the manager adopts anything answering
        # ``pong`` with no version handshake — see gatewayd's capability
        # comment) still runs a ``PoolKey.from_register`` that hard-requires
        # ``user_identity``. Omitting the key would make that daemon reject
        # every new stub's register as malformed, silently un-pooling the
        # whole install until the daemon restarts. A current daemon ignores
        # the key. Safe to drop once no daemon predating the key can be adopted.
        "user_identity": caller["principal_id"] or "unknown",
        "channel_id": channel_id,
        "config_snapshot_hash": _CONFIG_SNAPSHOT_PLACEHOLDER,
        "caller": caller,
        # Claim-push (gateway → gatewayd ``claim`` frame): the ancestor PID
        # chain of this stub, nearest first. gatewayd indexes the connection
        # under EVERY ancestor so a claim naming any level of the runtime's
        # process tree hits. The chain matters because the PID the gateway
        # records for a runtime (``AcpClient._process.pid``) can sit several
        # layers above the stub's immediate parent — e.g.
        # sandbox-wrapper → kiro-cli → kiro-cli-chat → stub — and a
        # single-PID index would never match (found live: claim frames
        # applied to 0 connections).
        "ancestor_pids": _ancestor_pids(),
        # Flat mirror — gatewayd accepts either shape; flat wins on
        # log/diff tooling legibility.
        "session_key": caller["session_key"],
        "session_type": caller["session_type"],
        "principal_id": caller["principal_id"],
    }
    if session_token:
        # Sibling field, deliberately NOT a PoolKey dimension: a per-connection
        # value in the key would give every session its own backend and pooling
        # would silently stop (see the ``pool`` module docstring). The token says
        # WHICH session this connection belongs to, never which backends are
        # interchangeable.
        payload["stub_session_token"] = session_token
    return payload


async def _write_frame(writer: asyncio.StreamWriter, obj: dict) -> None:
    # stdin_pump and _recaller_loop both write this shared stub->gateway socket
    # and both await drain(). Each write() here lands a WHOLE frame in one
    # synchronous call, so frame bytes cannot interleave — the hazard is the
    # concurrent drain(): under backpressure, FlowControlMixin._drain_helper on
    # many deployed interpreter patch releases holds a single drain waiter
    # (`assert waiter is None or waiter.cancelled()`), so a second concurrent
    # drain() trips the assert, kills that pump task, and tears the bridge down
    # (newer patch releases allow multiple waiters; older ones are common).
    # Serialize the write+drain pair through the writer's lock — the same
    # _mc_write_lock idiom gatewayd/backend use on their writers — which also
    # keeps the invariant robust if a future edit splits a frame across writes.
    lock = getattr(writer, "_mc_write_lock", None)
    guard = lock if lock is not None else contextlib.nullcontext()
    async with guard:
        writer.write(json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n")
        await writer.drain()


async def _read_frame(reader: asyncio.StreamReader) -> Optional[dict]:
    try:
        line = await reader.readuntil(b"\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None
    # errors="replace": an invalid-UTF-8 byte must NOT raise UnicodeDecodeError
    # (a ValueError, not json.JSONDecodeError) out through the handshake /
    # ensure_backend catch sets — that would kill the stub before fallback_exec.
    # A replaced char just yields a JSONDecodeError below, which IS caught and
    # degrades cleanly to a per-session exec.
    return json.loads(line.decode("utf-8", errors="replace")) if line else None


async def _safe_close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


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


def must_degrade_nonce_blind(
    server: str, poolable: bool, capabilities: list[str]
) -> bool:
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
    if server not in POOLING_REQUIRES_TENANT_NONCE:
        return False
    return poolable and "tenant_nonce" not in capabilities


class FallbackRequestedError(Exception):
    """Handshake cannot complete; caller must exec the real backend."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def handshake(
    socket_path: str, payload: dict
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str, dict]:
    """Connect + Register. Raises :class:`FallbackRequestedError` for any
    gateway-unavailable condition (connect refused, socket missing,
    rejected register, unexpected reply)."""
    try:
        reader, writer = await transport.connect(
            socket_path, limit=READ_BUFFER_LIMIT_BYTES,
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
    if not isinstance(resp, dict):
        # _read_frame returns raw json.loads output, which can be a non-dict
        # (list / number / string) for a malformed broker reply. resp.get(...)
        # would then raise AttributeError OUTSIDE the caught
        # (OSError, ConnectionError, json.JSONDecodeError) set above, crashing
        # the stub before fallback_exec and defeating the always-degrade-to-
        # per-session guarantee. Treat it as a fallback-eligible bad reply.
        await _safe_close(writer)
        raise FallbackRequestedError(
            f"unexpected handshake reply (not an object): {type(resp).__name__}"
        )

    msg_type = resp.get("type")
    if msg_type == "registered":
        return reader, writer, payload["stub_uuid"], resp
    await _safe_close(writer)
    if msg_type == "rejected":
        raise FallbackRequestedError(f"gateway rejected: {resp.get('reason', '?')}")
    raise FallbackRequestedError(f"unexpected handshake reply: type={msg_type!r}")


class StubSession:
    """Per-STUB state that must outlive any single gateway connection.

    A connection dies with the daemon; the stub does not. Everything a reconnect
    needs therefore lives here rather than inside :func:`run_bridge`'s frame,
    which is scoped to one socket:

    * **the stdin reader thread and its queue.** This is why a reconnect cannot
      simply call ``run_bridge`` again on a fresh socket. Creating the reader
      per call would put two threads on fd 0 -- splitting kiro-cli's lines
      between two consumers -- while any line the first one had already
      dequeued dies with the old frame. The head buffer
      (:meth:`push_front`) is the same argument one level up: the drain that
      runs while the bridge is down dequeues lines to judge them, and a frame it
      chose not to answer belongs to the stream, not to that task.
    * **the ``initialize`` frame.** The stdin pump consumes and forwards it
      once and kiro-cli never re-sends it, so without a copy here a fresh daemon
      would hold a never-initialized backend that rejects every later call --
      the reason the liveness path deliberately does not exec a replacement.
    * **the ``initialize`` result**, so a reconnect can refuse a daemon
      generation that would answer this session differently from the toolset it
      froze at ``session/new``.
    * **why the last bridge ended**, which is what decides reconnect vs exit.
    """

    #: Bridge endings a reconnect may follow. Everything else is a real
    #: shutdown -- kiro-cli closed stdin, a signal arrived, or kiro-cli's own
    #: reader died -- where re-attaching would serve nobody.
    RECONNECTABLE = frozenset({"peer_dead", "socket_eof", "socket_write_failed"})

    def __init__(self) -> None:  # noqa: D107
        #: Why the most recent bridge ended; see :attr:`RECONNECTABLE`.
        self.reason: str = ""
        #: Request IDs still unanswered when that bridge ended.
        self.outstanding_ids: list = []
        #: Verbatim ``initialize`` line as kiro-cli sent it.
        self.captured_init: Optional[bytes] = None
        #: The ``result`` object this session was told at handshake time.
        self.init_result: Optional[dict] = None
        #: Successful reconnects so far, for logging and the audit record.
        self.reconnects: int = 0
        self._subscribed: bool = False
        self._line_q: Optional["asyncio.Queue[bytes]"] = None
        #: Lines already taken off the queue that no pump has forwarded yet.
        #: :func:`_drain_while_disconnected` fills this while the bridge is down;
        #: :meth:`next_line` empties it before touching the queue, so the next
        #: bridge forwards them ahead of anything that arrives later.
        self._head: list[bytes] = []

    def note_outbound(self, line: bytes, msg: dict) -> None:
        """Record what a forwarded frame means for a future reconnect."""
        method = msg.get("method")
        if self.captured_init is None and method == "initialize":
            # Bounded by construction: one frame, replaced never, and only the
            # handshake matches -- so this cannot grow with session traffic.
            self.captured_init = line if line.endswith(b"\n") else line + b"\n"
        elif method == "resources/subscribe":
            # Latched on the REQUEST and never released. Counting on the request
            # is deliberate: a subscribe this session believes it holds is what
            # must not be dropped silently, and over-counting only costs a
            # reconnect that would have been safe.
            #
            # Never released even on ``resources/unsubscribe``, which is the
            # subtler half: that too is only a request, so an unsubscribe the
            # server REJECTED would clear the flag while the subscription stayed
            # live upstream -- and the reconnect would then be allowed for a
            # session whose resource updates are about to stop with nothing
            # said. Confirming it would mean tracking responses per id, which is
            # more machinery than the recovery is worth while replaying
            # subscriptions is still a follow-up.
            self._subscribed = True

    def has_live_subscriptions(self) -> bool:
        """Has this session asked for resource subscriptions a restart loses?

        The daemon's subscription table lives in its process and dies with it, so
        a reconnect that replayed only ``initialize`` would come back looking
        healthy while resource updates never arrived again. That is a silent
        degradation this fix must not introduce, so a subscribed session is
        refused the reconnect and takes the pre-existing terminal exit instead --
        the same outcome it had before, rather than a new quiet one.
        """
        return self._subscribed

    def note_init_result(self, msg: dict) -> None:
        """Remember the handshake result the FIRST daemon gave this session."""
        if self.init_result is None and isinstance(msg.get("result"), dict):
            self.init_result = msg["result"]

    def captured_init_id(self) -> Any:
        """The request id of the captured handshake, or ``None``."""
        if self.captured_init is None:
            return None
        try:
            msg = json.loads(self.captured_init)
        except (ValueError, TypeError):
            return None
        return msg.get("id") if isinstance(msg, dict) else None

    async def next_line(self) -> bytes:
        """One line from the real ``sys.stdin``, via a single reader thread.

        The read runs on a DEDICATED DAEMON thread and hands lines over through
        a bounded queue. A daemon thread is never joined at interpreter or
        asyncio shutdown, so a ``readline`` still blocked because kiro-cli holds
        stdin open cannot hang graceful SIGTERM -- the older
        ``run_in_executor(readline)`` used the default executor, which
        ``asyncio.run()`` joins via ``shutdown_default_executor()``.

        Lines put back by :meth:`push_front` come out first, in the order
        kiro-cli sent them. That is what lets the disconnected drain inspect a
        frame without consuming it: whatever it did not answer is handed to the
        next bridge ahead of anything newer, so a reconnect never reorders the
        stream.
        """
        if self._head:
            return self._head.pop(0)
        if self._line_q is None:
            self._line_q = self._start_reader()
        return await self._line_q.get()

    def push_front(self, lines: list[bytes]) -> None:
        """Return dequeued lines to the head of the inbound stream, in order.

        Called from the drain's ``finally`` so a cancellation cannot strand the
        frames it was holding: they belong to kiro-cli's stream, not to the task
        that happened to be looking at them.
        """
        if lines:
            self._head = list(lines) + self._head

    def take_pending_request_ids(self) -> list:
        """Ids of every request nobody will forward, dropping the frames.

        For the terminal path only. These frames are normally forwarded by the
        next bridge, but when the reconnect gives up there is no next bridge, so
        leaving them would mean those calls are never answered for the rest of a
        session whose stdout is about to close. The ids join
        :attr:`outstanding_ids` and are answered by the terminal
        :func:`_emit_error_frames` -- the same single-response rule as everywhere
        else, which is why the frames are dropped as the ids are taken.

        BOTH places a line can be waiting are read, and reading only one is a hole
        rather than an omission: the head holds what the drain handed back, while
        the queue holds whatever the reader thread has put there since -- a
        pipelined burst, or anything that arrived in the window between the drain
        being awaited and this call. A line left in the queue is a request that no
        longer has any reader at all.

        A buffered ``initialize`` is included rather than deferred: deferral
        exists so the handshake replay can answer it for real, and by the time
        this is called no replay is coming.

        The reader thread may still put another line in after this returns. That
        one is genuinely unanswerable -- the process is on its way out and stdout
        is about to close -- and no bookkeeping here can change it.
        """
        lines = list(self._head)
        self._head = []
        if self._line_q is not None:
            while True:
                try:
                    lines.append(self._line_q.get_nowait())
                except asyncio.QueueEmpty:
                    break
        ids = []
        for line in lines:
            try:
                msg = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(msg, dict) and "method" in msg and "id" in msg:
                ids.append(msg["id"])
        return ids

    def _start_reader(self) -> "asyncio.Queue[bytes]":
        loop = asyncio.get_running_loop()
        # Bounded + backpressured: the reader thread blocks on put() (via
        # run_coroutine_threadsafe) when the queue is full, so a stalled
        # writer.drain() (gatewayd slow to accept) cannot let the reader keep
        # draining stdin into an unbounded queue and balloon RSS.
        line_q: "asyncio.Queue[bytes]" = asyncio.Queue(
            maxsize=_STDIN_QUEUE_MAXSIZE
        )

        def _blocking_reader() -> None:
            fh = sys.stdin.buffer
            try:
                while True:
                    chunk = fh.readline()
                    # Block this thread until the queue has room. .result()
                    # waits on the loop; if the loop has stopped (shutdown) it
                    # raises and the daemon thread simply exits.
                    asyncio.run_coroutine_threadsafe(
                        line_q.put(chunk), loop
                    ).result()
                    if not chunk:
                        return
            except Exception:  # pragma: no cover — defensive
                try:
                    asyncio.run_coroutine_threadsafe(
                        line_q.put(b""), loop
                    ).result(timeout=1.0)
                except Exception:
                    pass

        threading.Thread(
            target=_blocking_reader, name="stub-stdin", daemon=True
        ).start()
        return line_q


async def run_bridge(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    stop_event: asyncio.Event,
    *,
    stdin: Optional[asyncio.StreamReader] = None,
    stdout_writer: Optional[asyncio.StreamWriter] = None,
    ping_interval: float = _BRIDGE_PING_INTERVAL_SECS,
    ping_max_misses: int = _BRIDGE_PING_MAX_MISSES,
    peer_supports_ping: bool = False,
    session: StubSession,
) -> None:
    """Pump stdin ↔ socket until either side closes or ``stop_event`` fires.

    ``stdin``/``stdout_writer`` are dependency-injected so tests can drive
    the bridge through in-process pipes; ``None`` falls back to real
    ``sys.stdin``/``sys.stdout``. On clean stdin EOF an ``Unregister``
    frame is sent so gatewayd detaches without waiting on refcount.

    ``session`` is the SINGLE channel this reports through, and it carries the
    state that must survive THIS connection so the caller can re-attach to a
    restarted gateway: pass the same :class:`StubSession` across successive calls
    and the stdin reader, the captured handshake and the ending all persist.
    ``session.reason`` names the ending precisely -- which is what distinguishes
    a transport loss worth reconnecting from a real shutdown -- and
    ``session.outstanding_ids`` lists what was left unanswered. It is required
    rather than defaulted because there is deliberately no return value: a
    caller without a session would learn nothing, and a second spelling of "why
    the bridge ended" would have to be kept in step with the first forever.
    """

    # Writer-thread liveness signals. The real bridge hands stdout frames to a
    # daemon writer thread (see stdout_pump); if that thread dies (broken pipe
    # to the kiro-cli reader) the bridge must tear down even when NO further
    # upstream line ever arrives to trip the producer-side check — otherwise
    # stdout_pump parks in reader.readuntil() forever and the bridge hangs, the
    # very leak this guards against. The threading.Event is polled by the
    # producer (_emit) as a fast path; the asyncio.Event, set via
    # call_soon_threadsafe from the writer thread, wakes a dedicated bridge task
    # so teardown never depends on upstream traffic.
    bridge_loop = asyncio.get_running_loop()
    # Per-CONNECTION fields, cleared so a reconnect is not judged on the last
    # connection's ending. The captured handshake and the stdin reader are
    # deliberately NOT cleared: those are what a reconnect exists to reuse.
    session.reason = ""
    session.outstanding_ids = []
    # stdin_pump and _recaller_loop both write this shared stub->gateway socket;
    # serialize their write+drain through one lock (mirrors gatewayd/backend).
    if getattr(writer, "_mc_write_lock", None) is None:
        setattr(writer, "_mc_write_lock", asyncio.Lock())
    writer_failed = threading.Event()
    writer_failed_evt = asyncio.Event()

    def _flag_writer_failed() -> None:
        writer_failed.set()
        bridge_loop.call_soon_threadsafe(writer_failed_evt.set)

    # --- Liveness state (ping-while-outstanding) ----------------------------
    # Track JSON-RPC request IDs forwarded to the gateway that have not yet
    # received a response. The liveness monitor fires ONLY while this set is
    # non-empty, so idle/slow-but-alive sessions are never timed out.
    _outstanding_ids: set = set()
    # Pong receipt flag: set by the stdout pump when a pong arrives, cleared
    # by the monitor each tick. Lightweight alternative to a counter/queue.
    _pong_received = asyncio.Event()
    # Peer-dead event: set by the monitor when consecutive pings go unanswered.
    _peer_dead_evt = asyncio.Event()

    async def stdin_pump() -> None:
        # Inbound line source. Tests inject an in-process StreamReader; the real
        # stub reads sys.stdin through the SESSION, which owns exactly one
        # reader thread per process. Owning it here instead would put a second
        # thread on fd 0 the moment a reconnect ran this pump again, splitting
        # kiro-cli's lines between two consumers (see StubSession).
        async def _next_line() -> bytes:
            if stdin is not None:
                try:
                    return await stdin.readuntil(b"\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    return b""
            return await session.next_line()

        while True:
            line = await _next_line()
            if not line:
                session.reason = session.reason or "stdin_eof"
                try:
                    await _write_frame(writer, {"type": "unregister"})
                except Exception:
                    pass
                return
            # Track outbound JSON-RPC request IDs (have "method" + "id"), and
            # let the session keep whatever a reconnect will need.
            # Best-effort: parse failures are silently ignored — the frame is
            # still forwarded verbatim.
            try:
                msg = json.loads(line)
                if isinstance(msg, dict):
                    session.note_outbound(line, msg)
                    if "method" in msg and "id" in msg:
                        _outstanding_ids.add(msg["id"])
            except (ValueError, TypeError):
                pass
            try:
                # Serialize with _recaller_loop's _write_frame writes on the
                # same socket: the write itself is whole-frame atomic, but a
                # second concurrent drain() under backpressure trips the
                # single-waiter assert in FlowControlMixin._drain_helper on
                # pre-3.12-fix interpreters, killing this pump task and tearing
                # the bridge down (see _write_frame).
                _lock = getattr(writer, "_mc_write_lock", None)
                _guard = _lock if _lock is not None else contextlib.nullcontext()
                async with _guard:
                    writer.write(line if line.endswith(b"\n") else line + b"\n")
                    await writer.drain()
            except (ConnectionError, BrokenPipeError):
                session.reason = session.reason or "socket_write_failed"
                return

    async def stdout_pump() -> None:
        stdout_fh = sys.stdout.buffer
        # Real path: hand frames to a DEDICATED daemon writer thread rather than
        # asyncio.to_thread (the default executor). asyncio.run() joins the
        # default executor via shutdown_default_executor(), so a write blocked
        # on a stalled kiro-cli reader (its stdout pipe buffer full) would hang
        # graceful SIGTERM. A daemon thread is never joined — mirroring the
        # stdin reader decoupling above. The queue is bounded so a stalled
        # reader can't balloon RSS; the pump applies backpressure with a
        # cancellable sleep, never the default executor.
        write_q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=256)
        writer_thread: Optional[threading.Thread] = None
        # writer_failed (threading.Event) + writer_failed_evt (asyncio.Event)
        # are defined at run_bridge scope; _flag_writer_failed() sets both so a
        # dead writer surfaces both to the producer (_emit fast path) and to a
        # dedicated bridge task, tearing the bridge down even if upstream goes
        # silent.
        if stdout_writer is None:
            def _blocking_writer() -> None:
                try:
                    while True:
                        item = write_q.get()
                        if item is None:
                            return
                        try:
                            stdout_fh.write(item)
                            stdout_fh.flush()
                        except Exception:
                            # Real write failure (reader gone / pipe broken):
                            # flag it so _emit raises and the bridge tears down
                            # even with no more upstream lines, then exit.
                            _flag_writer_failed()
                            return
                except Exception:  # pragma: no cover — defensive
                    _flag_writer_failed()
            writer_thread = threading.Thread(
                target=_blocking_writer, name="stub-stdout", daemon=True
            )
            writer_thread.start()

        async def _emit(line: bytes) -> None:
            if stdout_writer is not None:
                stdout_writer.write(line)
                await stdout_writer.drain()
                return
            # Bounded put with cancellable backpressure — never the default
            # executor, so a stalled writer thread cannot hang SIGTERM.
            while True:
                if writer_failed.is_set():
                    # Writer thread died — propagate instead of parking on a
                    # full queue forever so the bridge tears down.
                    raise BrokenPipeError("stub stdout writer thread died")
                try:
                    write_q.put_nowait(line)
                    return
                except queue.Full:
                    await asyncio.sleep(0.05)

        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    session.reason = session.reason or "socket_eof"
                    return
                if not line:
                    session.reason = session.reason or "socket_eof"
                    return
                # Intercept control frames (gateway liveness reply, gateway
                # keepalive) and track response IDs to clear outstanding
                # requests.
                # Best-effort: parse failures pass the line through verbatim.
                _is_control = False
                try:
                    msg = json.loads(line)
                    if isinstance(msg, dict):
                        _mtype = msg.get("type")
                        if _mtype == _BRIDGE_PONG_TYPE:
                            _pong_received.set()
                            _is_control = True
                        elif _mtype == _BRIDGE_KEEPALIVE_TYPE:
                            # Gateway-side transport probe. Nothing to do: the
                            # gateway learns what it needs from whether the
                            # write succeeded. Swallow it.
                            _is_control = True
                        elif _mtype == _SPAWN_QUEUED_TYPE:
                            # Our backend is being respawned behind the spawn
                            # gate. The daemon is alive and still holds our
                            # place, which is exactly what a pong proves, so it
                            # satisfies the liveness monitor too.
                            _pong_received.set()
                            _is_control = True
                        elif "id" in msg and "method" not in msg:
                            # A response (has id, no method) — clear from
                            # outstanding set.
                            _outstanding_ids.discard(msg["id"])
                            if msg["id"] == session.captured_init_id():
                                # What this session was told at handshake time.
                                # A later daemon generation answering the
                                # replayed handshake differently must not be
                                # passed off as the same server.
                                session.note_init_result(msg)
                except (ValueError, TypeError):
                    pass
                # Control frames are gateway<->stub only; never forward to
                # kiro-cli stdout.
                if _is_control:
                    continue
                try:
                    await _emit(line)
                except (ConnectionError, BrokenPipeError):
                    session.reason = session.reason or "writer_failed"
                    return
        finally:
            # Best-effort stop signal; the daemon thread is never joined, so
            # shutdown never blocks on it regardless.
            if writer_thread is not None:
                try:
                    write_q.put_nowait(None)
                except queue.Full:
                    pass

    async def _liveness_monitor() -> None:
        """Ping the gateway ONLY while requests are outstanding, and declare the
        peer dead after ``ping_max_misses`` consecutive unanswered pings.

        Every cycle consumes exactly ONE ``ping_interval``, whichever way it
        ends: a MISSED pong consumes it as the wait itself, and an ANSWERED pong
        consumes what is left of it as a sleep. So the advertised grace is
        ``ping_interval × ping_max_misses`` rather than twice that, and the ping
        RATE is one per interval rather than one per round-trip. That remainder
        sleep is load-bearing: the gateway answers a ping inline in its
        connection handler, so a healthy pong is back in microseconds and a loop
        that returned straight to the next ping would ping at socket speed for
        the whole life of an outstanding request -- burning a core on this stub
        and on the single-loop daemon that has to answer every one of them.
        An idle bridge also resets the miss count, so an earlier partial streak
        cannot carry across an idle gap.

        Never fires on an idle bridge, nor on a peer that answers while still
        working — that is the distinction between "slow" and "wedged", and the
        reason a blanket bridge timeout is the wrong mechanism here.
        """
        consecutive_misses = 0
        while not stop_event.is_set() and not _peer_dead_evt.is_set():
            if not _outstanding_ids:
                # Idle: burn one interval, then re-check. A stop during the wait
                # exits cleanly.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=ping_interval)
                    return
                except asyncio.TimeoutError:
                    pass
                consecutive_misses = 0
                continue
            # Clear the pong flag before sending so a reply to THIS ping is what
            # satisfies the wait below, not a stale one.
            _pong_received.clear()
            try:
                await _write_frame(writer, {"type": _BRIDGE_PING_TYPE})
            except (OSError, ConnectionError, BrokenPipeError):
                # Socket already broken — bridge will tear down on its own.
                return
            # Stamped AFTER the write, not before: _write_frame awaits the
            # shared writer lock and drain, so a stamp taken first would count
            # that backpressure against the interval and let the next ping
            # follow the pong immediately.
            sent_at = bridge_loop.time()
            # We check immediately after sending: the pong may have arrived
            # between our clear and the send (or during the write).
            # Give the peer the full next interval to reply.
            try:
                await asyncio.wait_for(
                    _pong_received.wait(), timeout=ping_interval
                )
            except asyncio.TimeoutError:
                pass
            if _pong_received.is_set():
                consecutive_misses = 0
                # Answered: the wait above was NOT the interval, so sleep out
                # the rest of it before the next ping. Measured from the ping on
                # the wire, so a slow-but-answered pong shortens this sleep
                # instead of adding to it, keeping the rate at one ping per
                # interval no matter how fast the peer replies. A stop wakes us
                # at once, so teardown never waits out a gap. ``_peer_dead_evt``
                # needs no waiter here: this task is its only setter and it
                # returns immediately after setting it, so it cannot change
                # under us.
                remaining = ping_interval - (bridge_loop.time() - sent_at)
                if remaining > 0:
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(), timeout=remaining
                        )
                        return
                    except asyncio.TimeoutError:
                        pass
            else:
                consecutive_misses += 1
                logger.warning(
                    "stub liveness: ping unanswered (%d/%d), "
                    "outstanding_ids=%d",
                    consecutive_misses,
                    ping_max_misses,
                    len(_outstanding_ids),
                )
                if consecutive_misses >= ping_max_misses:
                    logger.error(
                        "stub liveness: peer dead after %d missed pings; "
                        "triggering fallback",
                        consecutive_misses,
                    )
                    _peer_dead_evt.set()
                    return

    tasks = {
        asyncio.create_task(stdin_pump(), name="kirocrew-mcp-stub-stdin"),
        asyncio.create_task(stdout_pump(), name="kirocrew-mcp-stub-stdout"),
        asyncio.create_task(stop_event.wait(), name="kirocrew-mcp-stub-stop"),
        # Wakes when the stdout writer thread dies, so the bridge tears down
        # even if no further upstream line arrives to trip _emit's fast-path
        # check.
        asyncio.create_task(
            writer_failed_evt.wait(), name="kirocrew-mcp-stub-writer-failed"
        ),
    }
    # Gated on negotiation, not assumed: an older gatewayd has no ping handler,
    # so pinging it would guarantee a miss streak and force-degrade a healthy
    # session. No capability, no monitor — the bridge behaves exactly as before.
    if peer_supports_ping:
        tasks.add(
            asyncio.create_task(
                _liveness_monitor(), name="kirocrew-mcp-stub-liveness"
            )
        )
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _safe_close(writer)
        # Ending reason, most authoritative cause first. A dead peer also breaks
        # the stdin pump's next write, so the pump-set reason must not mask it;
        # and a deliberate stop outranks every transport symptom it produces.
        if stop_event.is_set():
            session.reason = "stop"
        elif _peer_dead_evt.is_set():
            session.reason = "peer_dead"
        elif writer_failed.is_set():
            session.reason = "writer_failed"
        elif not session.reason:
            # No pump claimed an ending. Unknown is deliberately NOT
            # reconnectable: re-attaching on a cause nobody can name is how a
            # recoverable outage turns into a session that answers wrongly.
            session.reason = "unknown"
        session.outstanding_ids = list(_outstanding_ids)


async def _emit_error_frames(req_ids: list, message: str, *, pool_label: str) -> None:
    """Answer abandoned requests so the caller is never left parked.

    The write runs on a worker thread and is bounded by
    ``_ERROR_EMIT_TIMEOUT_SECS``: a blocked kiro-cli reader (full stdout pipe)
    must not wedge the very path whose job is to unwedge the caller, which is
    the defect this path exists to prevent.
    """
    if not req_ids:
        return

    def _write() -> None:
        for req_id in req_ids:
            err_frame = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": message},
            }
            sys.stdout.buffer.write(
                json.dumps(err_frame, separators=(",", ":")).encode("utf-8") + b"\n"
            )
        sys.stdout.buffer.flush()

    try:
        await asyncio.wait_for(
            asyncio.to_thread(_write), timeout=_ERROR_EMIT_TIMEOUT_SECS
        )
    except (asyncio.TimeoutError, OSError, ValueError):
        # Nothing better to do: the reader is gone or wedged. Exiting still
        # closes stdout, which is what tells kiro-cli this server is done.
        logger.warning("could not emit error frames pool=%s", pool_label)


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
            try:
                msg = json.loads(line.decode("utf-8", errors="replace"))
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, dict):
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
        return await asyncio.wait_for(
            _await_reply(), timeout=_REPLAY_INIT_TIMEOUT_SECS
        )
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
        except (OSError, ConnectionError, json.JSONDecodeError):
            return _ADMIT_CLOSED, None
        if msg is None:
            return _ADMIT_CLOSED, None
        if not isinstance(msg, dict):
            continue
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


def _keep_socket_across_exec(writer: asyncio.StreamWriter) -> None:
    """Let the daemon see this connection until the exec'd backend EXITS.

    A fallback exec replaces this process with the real MCP server; the daemon
    charged that server to its host budget when it sent the fallback rejection
    and releases the charge when this connection reaches EOF. Python opens
    sockets non-inheritable, so without this the EOF would come at the exec
    itself and the fallback would be free. The backend inherits one extra
    descriptor it never touches; the kernel closes it when the process tree
    exits. Best-effort: a transport without a socket (a Windows pipe, where the
    stub stays alive as the backend's parent anyway) simply keeps today's shape.
    """
    try:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        os.set_inheritable(sock.fileno(), True)
    except (OSError, ValueError, AttributeError):
        logger.debug("could not keep the gateway socket across exec", exc_info=True)


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
        try:
            msg = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(msg, dict) or "method" not in msg or "id" not in msg:
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
    "Gateway restarted and is not back yet; this call was failed so it would "
    "not hang. Retry it."
)


def _no_room_to_hold(
    line: bytes, held: list, held_bytes: int
) -> bool:
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
                await _emit_error_frames(
                    expired, _QUEUED_FAIL_MESSAGE, pool_label=pool_label
                )
                logger.info(
                    "stub reconnect: failed %d call(s) queued while disconnected "
                    "pool=%s",
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
                try:
                    msg = json.loads(line)
                except (ValueError, TypeError):
                    msg = None
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
        if must_degrade_nonce_blind(
            str(payload.get("server_name") or ""), poolable, _caps
        ):
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
                    "stub reconnect: could not deliver the replayed handshake "
                    "pool=%s",
                    pool_label,
                )
                return None
            session.note_init_result(json.loads(forward))
        session.reconnects += 1
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


def _fallback_log_path() -> Path:
    return _crew_home() / "logs" / "stub_fallback.jsonl"


# Rotate the fallback log once it exceeds this size, keeping ONE previous
# generation (``.jsonl.1``). Unrotated it grows unbounded (467 KB in 15 h on
# one degraded host); a 1 MiB cap bounds total disk use at ~2 MiB
# while keeping enough history for the gateway's per-server fallback-rate
# aggregation (see ``gatewayd`` stats).
_FALLBACK_LOG_MAX_BYTES = 1024 * 1024


def log_fallback(
    reason: str,
    stub_uuid: str,
    pool_label: str,
    args: argparse.Namespace,
    *,
    terminal: bool = False,
) -> None:
    """Append one JSON record to the fallback audit log. OS errors are
    swallowed — logging failure must never block the exec that keeps
    kiro-cli working.

    ``terminal=True`` marks a record as a stub that DIED rather than degraded.
    Both events belong in this one log — same rotation, one place for an operator
    to look — but they must not be added together: :func:`fallback_counts` feeds
    gatewayd's ``stats``, and a fallback rate that silently includes terminals
    misreports whether pooling is engaging. The split is driven by this
    structured field, never by parsing the ``reason`` prefix, and a record
    written before the field existed has no key and so still counts as a
    fallback — which is what it was."""
    try:
        log_path = _fallback_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(),
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pid": os.getpid(),
            "stub_uuid": stub_uuid,
            "pool_label": pool_label,
            "reason": reason,
            "terminal": bool(terminal),
            "server": args.server,
            "agent": args.agent,
            "channel_id": args.channel_id or "",
            "target_command": args.target_command,
        }
        # Rotation (shared helper): O(1) rotate-by-rename at the cap, guarded
        # by a non-blocking try-lock so two stubs hitting the cap together
        # cannot both rotate, and a loser never waits — no log_fallback call
        # can stall its stub's event loop. The helper is best-effort by
        # contract (a missing file, a Windows sharing violation, or an
        # unopenable lock file degrades to not rotating), so the append below
        # always still runs; only a failure of the append itself may drop the
        # record, caught by this function's own handler.
        rotate_jsonl_at(log_path, _FALLBACK_LOG_MAX_BYTES)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        pass


# Sliding window for fallback aggregation: long enough to catch a slow drip
# (tens per hour), short enough that a fixed incident ages out of the count.
_FALLBACK_COUNT_WINDOW_SECS = 24 * 3600.0


def fallback_counts() -> dict[str, Any]:
    """Aggregate the fallback audit log into per-server counts.

    Reads the live log plus the one rotated generation (see
    ``_FALLBACK_LOG_MAX_BYTES``) and counts records whose ``ts`` falls within
    the last ``_FALLBACK_COUNT_WINDOW_SECS``. This is the reader the log never
    had: degradations accrued with no signal anywhere. gatewayd folds the
    result into its ``stats`` reply, making the per-server fallback rate
    queryable from the gateway control socket instead of the operator having
    to read a process tree.

    Returns ``{"window_secs": ..., "total": n, "by_server": {name: n},
    "by_reason": {reason: n}, "terminal_total": n,
    "terminal_by_server": {name: n}}``. Never raises — an unreadable or torn log
    yields the counts of whatever parsed.

    ``total`` / ``by_server`` / ``by_reason`` count DEGRADATIONS only. A record
    flagged ``terminal`` is a stub that died instead of degrading, which is a
    different event with a different remedy, so it is tallied separately rather
    than inflating the fallback rate this function exists to report. Records
    predating the flag have no key and count as fallbacks, which is what they
    were.
    """
    cutoff = time.time() - _FALLBACK_COUNT_WINDOW_SECS
    total = 0
    by_server: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    terminal_total = 0
    terminal_by_server: dict[str, int] = {}
    live = _fallback_log_path()
    for path in (live.with_suffix(".jsonl.1"), live):
        try:
            with open(path, "rb") as f:
                for line in bounded_records(f, path, label="stub_fallback"):
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # torn tail line from a racing writer
                    if not isinstance(rec, dict):
                        continue
                    ts = rec.get("ts")
                    if not isinstance(ts, (int, float)) or ts < cutoff:
                        continue
                    server = str(rec.get("server") or rec.get("pool_label") or "?")
                    if rec.get("terminal") is True:
                        terminal_total += 1
                        terminal_by_server[server] = (
                            terminal_by_server.get(server, 0) + 1
                        )
                        continue
                    total += 1
                    by_server[server] = by_server.get(server, 0) + 1
                    reason = str(rec.get("reason") or "?")
                    by_reason[reason] = by_reason.get(reason, 0) + 1
        except OSError:
            continue
    return {
        "window_secs": _FALLBACK_COUNT_WINDOW_SECS,
        "total": total,
        "by_server": by_server,
        "by_reason": by_reason,
        "terminal_total": terminal_total,
        "terminal_by_server": terminal_by_server,
    }


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
            await asyncio.wait_for(
                asyncio.shield(drain), timeout=_ERROR_EMIT_TIMEOUT_SECS + 1.0
            )
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain
        # Either way the drain has run its ``finally`` and handed back everything
        # it held before the next pump reads a line -- two readers on one queue is
        # what would reorder the stream.


async def alog_fallback(
    reason: str,
    stub_uuid: str,
    pool_label: str,
    args: argparse.Namespace,
    *,
    terminal: bool = False,
) -> None:
    """Run :func:`log_fallback` in a worker thread.

    The async bridge calls this before every terminal fallback: the write is
    plain blocking file I/O (open/append, plus the best-effort rotation), so
    it runs off the event loop, and awaiting it guarantees the record is on
    disk before the caller proceeds to ``fallback_exec`` (which replaces the
    process image and would otherwise race the write)."""
    await asyncio.to_thread(
        functools.partial(
            log_fallback, reason, stub_uuid, pool_label, args, terminal=terminal
        )
    )


def _inherited_std_fd(stream: Any, default: int) -> int:
    """The fd to hand a child for one of our own standard streams.

    ``sys.stdin`` and friends can be replaced or detached (pytest's capture, a
    ``pythonw`` host), in which case ``fileno()`` raises rather than answering.
    Fall back to the well-known number: this runs in a process kiro-cli spawned
    with real pipes on 0/1/2, so the constant is right whenever the object is
    not.
    """
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return default
    return fd if isinstance(fd, int) and fd >= 0 else default


def _child_exit_status(rc: object) -> int:
    """The status to exit with after relaying a Windows child's own.

    Windows reports an exit code as an unsigned ``DWORD``, and a crash lands far
    above ``INT_MAX``: an access violation is ``0xC0000005``, i.e. 3221225477.
    ``os._exit`` parses its argument as a C ``int``, so handing that straight
    over raises ``OverflowError`` -- the stub would die on an exception instead
    of relaying the status, which is the one thing this function exists to do,
    and it would do so on the ordinary signature of a crashing backend rather
    than in some corner.

    Reinterpret such a value as signed 32-bit instead of clamping it. The OS
    reads the low 32 bits back out, so the code kiro-cli observes is the exact
    ``DWORD`` the backend exited with -- a clamp would silently rewrite a crash
    into some unrelated status. Anything still outside a C ``int``, or not an
    int at all, becomes 1: unrepresentable, so report plain failure.
    """
    if not isinstance(rc, int):
        return 1
    if rc > 0x7FFFFFFF:
        rc -= 0x100000000
    return rc if -0x80000000 <= rc <= 0x7FFFFFFF else 1


def _fallback_spawn_child(argv: list[str], exec_env: dict[str, str]) -> NoReturn:
    """Stand in for ``execvpe`` on Windows, which has no in-place exec.

    CPython documents the replacement as in-place *"On Unix"*, where the image
    is loaded into this process and keeps its pid. Windows has no such call, so
    the ``exec*`` family is emulated as spawn-then-exit: the backend comes up
    under a NEW pid and THIS process dies. kiro-cli owns this process's stdio
    pipe and waits on the pid it spawned, so that exit reads to it as the server
    hanging up -- it reports ``connection closed: initialize response`` and the
    server's whole tool surface is missing from the session. The degrade path was
    fatal on the one platform it existed to rescue, and every stubbed server
    failed while every directly-launched one worked.

    Run the backend as a CHILD and stay alive as its parent instead. Its stdin,
    stdout and stderr are this process's own fds, duplicated into it explicitly
    (``close_fds`` blocks handle inheritance on Windows, so passing the numbers
    is what guarantees the child gets the real pipe rather than a fresh
    console). kiro-cli therefore talks to the real backend over the pipe it
    already holds, and the pid it waits on lives for the session. Every
    ``fallback_exec`` call site is reached with kiro-cli's ``initialize`` still
    unread -- the stub's own "clean per-session exec" invariant, asserted by the
    comments at each of those sites -- so the hand-off loses no buffered request.

    Resolve the command against the CHILD's ``PATH``, because that is what
    ``execvpe`` searches; Windows ``CreateProcess`` would search this process's
    instead, so a relative command could otherwise resolve differently on the
    two platforms. An unresolvable name is passed through unchanged so the
    resulting ``FileNotFoundError`` still surfaces, matching ``execvpe``.

    Exit through ``os._exit``: this stands in for a process replacement, which
    runs no cleanup handlers and flushes nothing, and a ``SystemExit`` raised
    here would have to survive the teardown of the stub's own event loop.
    """
    resolved = shutil.which(argv[0], path=exec_env.get("PATH")) or argv[0]
    # Our own buffers, not the child's: it inherits the same fds, so anything
    # still queued here would interleave into the middle of its JSON-RPC stream.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError, ValueError):
            stream.flush()
    proc = subprocess.Popen(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        [resolved, *argv[1:]],
        env=exec_env,
        stdin=_inherited_std_fd(sys.stdin, 0),
        stdout=_inherited_std_fd(sys.stdout, 1),
        stderr=_inherited_std_fd(sys.stderr, 2),
    )
    try:
        rc = proc.wait()
    except BaseException:
        # Do not leave the backend holding kiro-cli's pipe with nobody waiting on
        # it. A hard ``TerminateProcess`` on us cannot be intercepted at all, and
        # the child survives that; what saves the session there is the pipe
        # itself -- kiro-cli's exit closes fd 0 and an MCP server exits on stdin
        # EOF, exactly as a directly-launched one would.
        with contextlib.suppress(OSError):
            proc.terminate()
        raise
    os._exit(_child_exit_status(rc))


def fallback_exec(args: argparse.Namespace) -> None:
    """Replace the current process with the real MCP backend. ``execvpe``
    never returns on success; a return raises so the caller surfaces a
    diagnostic. Windows has no in-place exec, so there the backend runs as a
    child inheriting this process's stdio -- see :func:`_fallback_spawn_child`
    for why the emulated ``exec*`` would kill the session outright."""
    target_args = _resolve_target_args(args)
    argv = [args.target_command, *target_args]
    # Restore the server's declared env. The rewriter moves declared env
    # (which routinely holds tokens / API keys) into a 0600 sidecar the stub
    # otherwise reads only for PoolKey hashing. On this fallback path we exec
    # the real backend directly, so it must run with its declared env to match
    # the non-pooled baseline — the daemon's own environment lacks it.
    exec_env = dict(os.environ)
    # Never hand the backend this session's stub token. It is a bearer name for
    # the session's identity at gatewayd, and the process about to replace this
    # one is the operator's third-party server binary — which on a later gateway
    # start could register with it and be answered as this session. Its own
    # declared env is restored below; this one value was never part of it. The
    # non-fallback path is unaffected: gatewayd spawns backends from its OWN
    # environment, so the token has never reached one there.
    exec_env.pop(STUB_SESSION_TOKEN_ENV, None)
    exec_env.update(_parse_env_file(getattr(args, "env_file", "") or ""))
    if platform_compat.IS_WINDOWS:
        _fallback_spawn_child(argv, exec_env)
    # exec IS this fallback stub's whole purpose: when the gateway is
    # unavailable, replace this process with the operator's real MCP backend.
    # argv (target_command / target_args) and exec_env (the server's declared
    # env) both originate from the operator's own ~/.kiro/agents/*.json via the
    # rewriter — never from a peer or stub — so the tainted-input audit rule is
    # a false positive under this threat model.
    os.execvpe(argv[0], argv, exec_env)  # nosemgrep: python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args
    raise RuntimeError(f"execvpe({argv[0]!r}) returned unexpectedly")


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event
) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass


# --- Warm-pool caller repair -----------------------------------------------

# A warm-pool stub registers BEFORE its kiro-cli is claimed, so its Register
# payload carries an empty ``session_key`` (the key is unknown at pool-fill
# time). ``rekey()`` on claim only mutates the gateway-side provider object —
# it never re-registers this stub — so gatewayd keeps ``caller=None`` for the
# life of the connection and every state-mutating tool (``learn_add`` et al.)
# fails with "missing X-Session-Key". These knobs bound a poll that watches for
# the session key to materialize (the dashboard writes
# ``session_pid_<pid>.txt`` on the claimed session's first turn) and then sends
# a ``recaller`` control frame so gatewayd stamps the right identity from then
# on.
_RECALLER_POLL_INTERVAL_SECS = 1.5
# Backoff ceiling. Claim-push (gateway → gatewayd ``claim`` frame on rekey)
# is now the primary identity-repair path; this poll is the FALLBACK for
# claim-frame loss / gatewayd restarts, so it must never strand a connection
# by expiring — a warm-pool runtime is routinely claimed far later than any
# fixed budget, so a fixed deadline would strand exactly that case.
# Instead of a deadline, the interval decays from
# 1.5s to this cap, so a long-idle pool stub costs one identity probe every
# 30s instead of leaking an aggressive poll forever.
_RECALLER_POLL_MAX_INTERVAL_SECS = 30.0
_RECALLER_POLL_BACKOFF = 1.5


async def _recaller_loop(
    writer: asyncio.StreamWriter,
    channel_id: Optional[str],
    stop_event: asyncio.Event,
) -> None:
    """Poll for a late-arriving session key and re-register the caller once.

    Started only when the initial Register carried an empty ``session_key``.
    FALLBACK path under claim-push (the gateway's ``claim`` frame is the
    primary repair); unbounded with interval backoff so a late claim can
    never be stranded by a poll deadline. Exits on: key found (after sending
    one ``recaller`` frame) or bridge teardown (``stop_event``). Writes a
    whole frame per ``_write_frame`` (a single synchronous ``writer.write``
    before any await), so frame BYTES cannot interleave with the stdin pump
    sharing this ``writer`` — but the write lock in ``_write_frame`` is still
    required: two coroutines awaiting ``drain()`` concurrently under
    backpressure trip the single-drain-waiter assert in
    ``FlowControlMixin._drain_helper`` on pre-3.12-fix interpreters, which
    kills a pump task and tears the bridge down.
    """
    loop = asyncio.get_running_loop()
    interval = _RECALLER_POLL_INTERVAL_SECS
    while not stop_event.is_set():
        try:
            # Sleep-or-wake: return promptly if the bridge tears down.
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        interval = min(interval * _RECALLER_POLL_BACKOFF, _RECALLER_POLL_MAX_INTERVAL_SECS)
        # ``_build_caller_block`` -> ``CallerContext.from_env`` does a
        # synchronous /proc ancestry walk + file reads; offload it to the
        # dedicated subprocess pool (not the shared default) so a slow or
        # wedged filesystem read here can neither freeze the stdin-pump bridge
        # that shares this event loop nor starve unrelated default-pool work.
        caller = await loop.run_in_executor(
            subprocess_executor(), _build_caller_block, channel_id
        )
        if not caller["session_key"]:
            continue
        frame = {
            "type": "recaller",
            "caller": caller,
            # Flat mirror — gatewayd's ``_caller_from_register`` accepts either
            # the nested ``caller`` dict or these top-level fields.
            "session_key": caller["session_key"],
            "session_type": caller["session_type"],
            "principal_id": caller["principal_id"],
            "channel_id": channel_id,
        }
        try:
            await _write_frame(writer, frame)
            logger.info(
                "stub sent recaller after warm-pool claim (session_type=%s)",
                caller["session_type"],
            )
        except (OSError, ConnectionError):
            # Connection already gone — bridge will tear down on its own.
            pass
        return


async def _amain(argv: Optional[list[str]] = None) -> int:
    # ── Name the default executor ──
    # asyncio.to_thread and run_in_executor(None, ...) route onto the loop's
    # default executor, which Python names threads anonymously.  This names
    # them ``mc-default`` so profilers like py-spy can attribute blocking work
    # to this stub.  Must run BEFORE any to_thread offload.
    configure_default_executor()

    # An invalid MC_MCP_LOG (e.g. "verbose") would make basicConfig raise
    # "Unknown level" and kill the stub BEFORE its fallback-to-per-session-exec
    # path can run. Fall back to WARNING on any unrecognised level.
    _log_level = os.environ.get("KIROCREW_MCP_LOG", os.environ.get("MC_MCP_LOG", "warning")).upper()
    if not isinstance(logging.getLevelName(_log_level), int):
        _log_level = "WARNING"
    logging.basicConfig(
        level=_log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    args = _parse_args(argv)
    # build_register_payload -> _build_caller_block -> CallerContext.from_env
    # does a synchronous /proc ancestry walk + file reads (and _binary_version
    # hashes the target binary), so offload the whole cold-start resolution to
    # the dedicated subprocess pool (not the shared default) — consistent with
    # _recaller_loop, so a wedged filesystem read can't starve default-pool work.
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(subprocess_executor(), build_register_payload, args)

    try:
        pool_label = PoolKey.from_register(payload).human_readable()
    except ValueError as exc:
        # Defensive — our own payload should never be malformed.
        logger.warning("built malformed PoolKey payload: %s", exc)
        pool_label = f"{args.agent}:{args.server}"

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    try:
        reader, writer, stub_uuid, registered = await asyncio.wait_for(
            handshake(args.socket, payload),
            timeout=_HANDSHAKE_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        await alog_fallback("handshake_timeout", payload["stub_uuid"], pool_label, args)
        logger.warning("handshake timed out; falling back pool=%s", pool_label)
        fallback_exec(args)
        return 1  # unreachable
    except FallbackRequestedError as exc:
        await alog_fallback(exc.reason, payload["stub_uuid"], pool_label, args)
        logger.warning("handshake failed (%s); falling back pool=%s", exc.reason, pool_label)
        fallback_exec(args)
        return 1  # unreachable
    logger.info("registered stub_uuid=%s pool=%s", stub_uuid, pool_label)

    capabilities = registered.get("capabilities") if isinstance(registered, dict) else None
    _caps = capabilities if isinstance(capabilities, list) else []

    # A private backend is the one thing this connection cannot verify after the
    # fact. ``poolable`` is a register-payload field, so a daemon predating it
    # ignores the flag and routes this register through the shared index —
    # silently co-tenanting a server the operator never allowlisted. Such a
    # daemon is reachable: the manager adopts anything answering ``pong``, with
    # no version handshake, so one that outlived a package upgrade serves new
    # stubs. Degrade instead: the per-session exec IS the private topology this
    # connection asked for, and kiro-cli's ``initialize`` is still unread, so the
    # fallback is clean. A stub that DID ask to share needs no check — an old
    # daemon pools it, which is what it wanted.
    if must_degrade_unshareable(bool(args.poolable), _caps):
        reason = "gateway does not honour the poolable field"
        await alog_fallback(reason, payload["stub_uuid"], pool_label, args)
        logger.warning(
            "handshake: gateway did not advertise poolable_ack and this server is "
            "not shareable; falling back to a per-session exec rather than risk "
            "being pooled pool=%s",
            pool_label,
        )
        await _safe_close(writer)
        fallback_exec(args)
        return 1  # unreachable

    # The mirror case: pooling ASKED FOR, behind a daemon that mints no
    # per-connection nonce. This server separates the co-tenants the gateway
    # cannot name by that nonce, so without it one pooled backend holds one
    # namespace for all of them -- and the backend cannot tell that from the 1:1
    # topology where its per-process fallback is right. Same reachability as
    # above: the manager adopts any daemon answering ``pong``. Degrading is the
    # honest answer rather than a loss, because an exclusive backend is exactly
    # where that per-process fallback separates sessions correctly, and
    # ``initialize`` is still unread so the exec is clean.
    if must_degrade_nonce_blind(args.server, bool(args.poolable), _caps):
        reason = "gateway mints no per-connection tenant nonce"
        await alog_fallback(reason, payload["stub_uuid"], pool_label, args)
        logger.warning(
            "handshake: gateway did not advertise tenant_nonce and this server "
            "separates unnamed co-tenants by it; falling back to a per-session "
            "exec rather than pooling without it pool=%s",
            pool_label,
        )
        await _safe_close(writer)
        fallback_exec(args)
        return 1  # unreachable

    # B1 pre-flight: trigger the gateway's backend spawn with a
    # control frame BEFORE forwarding any real MCP traffic, so a capacity /
    # breaker rejection reaches us while kiro-cli's ``initialize`` is still
    # unread in fd0 (a clean per-session exec fallback). Gated on the gateway
    # advertising the ``ensure_backend`` capability: an OLD gateway without it
    # would treat the control frame as a real MCP frame and never reply, so we
    # skip the pre-flight and bridge directly (legacy lazy-spawn path, no 25s
    # skew penalty).
    if "ensure_backend" in _caps:
        queue_aware = "spawn_queue" in _caps
        outcome, ready = await _ensure_backend_admitted(
            reader,
            writer,
            queue_aware=queue_aware,
            total_budget_secs=_SPAWN_QUEUE_WAIT_BUDGET_SECS,
        )
        if outcome in (_ADMIT_TIMEOUT, _ADMIT_CLOSED):
            # Gateway unreachable / wedged mid-pre-flight -- same posture as a
            # connect failure: fall back to a direct per-session exec. A
            # queue-aware wait that ran out of budget lands here too: silence
            # for 25 s from a daemon that promised keepalives, or the whole
            # budget spent, is a daemon that is not serving, not one that is
            # full -- a full daemon says so with a ``capacity`` rejection.
            await _safe_close(writer)
            await alog_fallback(
                f"ensure_backend_io:{outcome}", payload["stub_uuid"], pool_label, args,
            )
            logger.warning("ensure_backend %s; falling back pool=%s", outcome, pool_label)
            fallback_exec(args)
            return 1  # unreachable
        if outcome == _ADMIT_REJECTED:
            assert ready is not None
            cls = _rejection_class(ready)
            reason = str(ready.get("reason") or "ensure_backend_rejected")
            if cls == _REJECT_CLASS_CAPACITY:
                # The host is full and the daemon said so after the whole wait
                # budget. No fallback -- an exec here is one more process on
                # the host that just refused one -- and no death, which
                # kiro-cli would read as the server crashing. Keep the stdio
                # transport open and answer each request with a typed error
                # the session can tell apart from a broken server.
                retry_after = ready.get("retry_after_secs")
                await _safe_close(writer)
                await alog_fallback(
                    f"capacity:{reason}", payload["stub_uuid"], pool_label, args, terminal=True,
                )
                logger.warning(
                    "gateway refused ensure_backend for capacity (%s, retry_after=%s); "
                    "answering requests with a typed error pool=%s",
                    reason, retry_after, pool_label,
                )
                refusal_session = StubSession()
                return await _serve_capacity_refusal(
                    refusal_session,
                    stop_event,
                    reason=reason,
                    retry_after_secs=retry_after if isinstance(retry_after, int) else None,
                    pool_label=pool_label,
                )
            fallback_eligible = (
                cls in _FALLBACK_CLASSES
                if cls is not None
                else (
                    bool(ready.get("fallback"))
                    # A legacy daemon cannot send the tag, so an untagged
                    # target-unknown is routed to the fallback-exec path
                    # rather than killing the server. See
                    # ``must_degrade_unknown_target``.
                    or must_degrade_unknown_target(reason)
                )
            )
            if not fallback_eligible:
                # Terminal rejection (genuine spawn failure): the gateway says
                # this server cannot run. Surface the failure instead of
                # exec'ing — matches the lazy path and avoids per-session
                # crash-loops of a broken backend.
                #
                # Audited BEFORE returning, and this is the whole point of the
                # record. A terminal exit writes no fallback record by
                # definition, and this ``logger.error`` goes to the stub's own
                # stderr, which kiro-cli swallows — so the pre-fix failure mode
                # was a server whose entire tool surface vanished from a session
                # while every durable log showed only a healthy ``registered``
                # line. On the daemon side a terminal and a served stub were
                # indistinguishable after the fact. The ``terminal:`` prefix
                # keeps these separable from real fallbacks in
                # ``stub_fallback_counts()``, which the gateway folds into its
                # ``stats`` reply.
                await _safe_close(writer)
                await alog_fallback(
                    f"terminal:{reason}",
                    payload["stub_uuid"], pool_label, args,
                    terminal=True,
                )
                logger.error(
                    "gateway terminally rejected ensure_backend (%s); not falling back pool=%s",
                    reason, pool_label,
                )
                return 1
            # Fallback-eligible rejection (``compat`` / ``isolation``, or a
            # legacy ``fallback: true``) -> exec the real backend directly for
            # this session. A queue-aware daemon charged that exec to its host
            # budget and releases it when this connection reaches EOF, so the
            # socket is kept open across the exec rather than closed here.
            if queue_aware:
                _keep_socket_across_exec(writer)
            else:
                await _safe_close(writer)
            await alog_fallback(reason, payload["stub_uuid"], pool_label, args)
            logger.warning(
                "gateway fallback-rejected ensure_backend (%s, class=%s); falling back pool=%s",
                reason, cls or "legacy", pool_label,
            )
            fallback_exec(args)
            return 1  # unreachable

    # One session across every connection this stub makes. The stdin reader, the
    # captured handshake and the ending reason all live here precisely because a
    # connection dies with the daemon and the stub does not.
    session = StubSession()
    peer_supports_ping = bool(
        isinstance(capabilities, list) and "bridge_ping" in capabilities
    )
    while True:
        # Warm-pool caller repair: if we registered without a session key (the
        # kiro-cli was pool-spawned before its session was claimed), watch for
        # the key to materialize and re-register the caller so state-mutating
        # tools (learn_add et al.) work for the claimed session. No-op for stubs
        # that already had a key at register. Re-armed per connection: the task
        # writes THIS socket, so one that outlived its connection would write a
        # closed writer.
        recaller_task: Optional[asyncio.Task[None]] = None
        if not payload.get("session_key"):
            recaller_task = asyncio.create_task(
                _recaller_loop(
                    writer, _resolve_channel_id(args.channel_id), stop_event
                ),
                name="kirocrew-mcp-stub-recaller",
            )
        try:
            # The return value is the legacy single-connection signal; this
            # caller reads the richer ending off the session instead.
            await run_bridge(
                reader,
                writer,
                stop_event,
                peer_supports_ping=peer_supports_ping,
                session=session,
            )
        finally:
            if recaller_task is not None:
                if not recaller_task.done():
                    recaller_task.cancel()
                # Always await — even a task that already finished (successfully
                # or with an exception) must have its result/exception
                # retrieved, or asyncio logs "Task exception was never
                # retrieved" and hides a bug.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await recaller_task

        if session.reason not in StubSession.RECONNECTABLE:
            break

        # The transport is gone but this process, its stdin and kiro-cli's frozen
        # toolset are not. Answer whatever was in flight, exactly once, and hold
        # back only what the replay below can still answer for real -- see
        # _split_abandoned_ids for why each half is treated the way it is.
        #
        # What happens to requests across the reconnect that follows:
        #
        # * IN-FLIGHT calls (forwarded to the dead daemon, unanswered) are failed
        #   HERE with a retryable -32603 before the reconnect starts. They are
        #   not replayed: the stub cannot know whether the daemon forwarded them
        #   upstream before dying, and replaying could double a side effect. The
        #   one exception is an unanswered ``initialize``, which the replay can
        #   still answer for real.
        # * NEWLY ARRIVING requests are held briefly, then failed. The stdin
        #   reader thread (``StubSession._start_reader``) keeps reading fd 0 into
        #   the session's bounded queue, and ``_drain_while_disconnected`` below
        #   is what reads that queue while no pump exists -- holding each frame
        #   for ``_QUEUED_GRACE_SECS`` so an ordinary restart still forwards and
        #   answers it for real, then failing whatever is left with the same
        #   retryable ``-32603``. Held frames go back to the head of the stream,
        #   so the next ``run_bridge`` forwards them in order and tracks their ids
        #   as outstanding from that point; a failed one is dropped, because its
        #   id has been answered. stdout stays open throughout, so kiro-cli never
        #   sees the transport close until the budget is genuinely spent.
        _to_fail, deferred_init_id = _split_abandoned_ids(session)
        await _emit_error_frames(
            _to_fail,
            "Gateway restarted; this call was failed so it would not hang. "
            "Retry it.",
            pool_label=pool_label,
        )
        session.outstanding_ids = (
            [] if deferred_init_id is None else [deferred_init_id]
        )

        if session.has_live_subscriptions():
            # Recovering the transport for a subscribed session would hand it
            # back a connection that answers calls but never delivers another
            # resource update: the daemon's subscription table died with the old
            # process. That is a NEW silent degradation, and trading a visible
            # outage for a quiet one is precisely what this fix exists to stop.
            # Refuse, and leave replaying subscriptions to the change that can
            # do it properly.
            logger.warning(
                "stub bridge ended (%s) with live resource subscriptions; "
                "refusing to reconnect because they cannot be re-established "
                "here pool=%s",
                session.reason,
                pool_label,
            )
            break

        if session.captured_init is None:
            # Nothing to re-prime a fresh daemon with, so a reconnect could only
            # produce a never-initialized backend that rejects every later call.
            logger.warning(
                "stub bridge ended (%s) before initialize was seen; no reconnect "
                "is possible pool=%s",
                session.reason,
                pool_label,
            )
            break

        # Refresh the caller before re-registering: the session key may have
        # materialized since the first Register (warm-pool claim), and carrying
        # it in the new handshake is better than re-running the recaller.
        #
        # The uuid in this payload is a placeholder: ``_reconnect`` mints a fresh
        # one per handshake ATTEMPT, because it is a per-REGISTRATION handle the
        # daemon keys attach, refcount and the private backend map on. A
        # reconnectable ending does not prove the daemon died -- a wedged peer or
        # a broken write leaves the predecessor registration in place -- so
        # reusing a handle would either attach twice under one or let the
        # predecessor's teardown tear down the replacement.
        try:
            payload = await loop.run_in_executor(
                subprocess_executor(), build_register_payload, args
            )
        except Exception as exc:  # noqa: BLE001 — never lose the reconnect to this
            logger.warning(
                "stub reconnect: keeping the previous register payload (%s)", exc
            )

        await alog_fallback(f"bridge_lost_{session.reason}", stub_uuid, pool_label, args)
        attached = await _reconnect_while_draining(
            args.socket,
            payload,
            session,
            stop_event,
            poolable=bool(args.poolable),
            pool_label=pool_label,
        )
        if attached is None:
            logger.warning(
                "stub could not re-attach to the gateway after %s; this session "
                "loses these servers pool=%s",
                session.reason,
                pool_label,
            )
            break
        reader, writer, registered, stub_uuid = attached
        # The audit and log records must name the registration that is actually
        # live: the reconnect loop mints one per ATTEMPT, so the payload this
        # frame holds is not necessarily the one that registered.
        capabilities = registered.get("capabilities")
        peer_supports_ping = bool(
            isinstance(capabilities, list) and "bridge_ping" in capabilities
        )

    # Terminal: the bridge ended for a reason no reconnect can address, or the
    # reconnect budget ran out.
    #
    # Deliberately NOT followed by fallback_exec. The four pre-flight fallback
    # sites work because kiro-cli's ``initialize`` is still unread in fd0, so the
    # exec'd server comes up initialized. Here stdin_pump has already consumed
    # and forwarded ``initialize``, and kiro-cli never re-sends it, so an exec'd
    # server would be a fresh, never-initialized MCP server that rejects every
    # subsequent call. Exec would convert "wedged" into "fast-failing", not into
    # "working" — reconnecting is what converts it into "working", and it has
    # already been tried by the time we get here.
    #
    # Anything the drain handed back that no bridge forwarded is answered here.
    # Normally the next ``run_bridge`` sends those frames on; this path has no
    # next bridge, so without this they would die with stdout unanswered -- the
    # very shape the drain exists to remove. Ids already listed are skipped: one
    # response per id, the same rule as everywhere else.
    for _held_id in session.take_pending_request_ids():
        if _held_id not in session.outstanding_ids:
            session.outstanding_ids.append(_held_id)
    if session.outstanding_ids:
        _abandoned = session.outstanding_ids
        # Dropped as they are answered: this is the last writer, but leaving them
        # listed would let any future path here emit a second response for an id
        # kiro-cli has already been given one for.
        session.outstanding_ids = []
        await _emit_error_frames(
            _abandoned,
            "Gateway stopped responding and could not be reached again; this "
            "call was failed so it would not hang. Start a new session to "
            "restore these tools.",
            pool_label=pool_label,
        )
        await alog_fallback(
            f"bridge_dead_{session.reason}", stub_uuid, pool_label, args
        )
        logger.warning(
            "bridge ended (%s) with %d outstanding call(s) after %d reconnect(s) "
            "pool=%s",
            session.reason,
            len(_abandoned),
            session.reconnects,
            pool_label,
        )
        return 1
    if session.reason in StubSession.RECONNECTABLE:
        # The transport was lost with nothing in flight, so no call needs an
        # answer — but the session still loses these servers, and without this
        # record that leaves no trace at all. Record it so a degraded session is
        # explicable afterwards instead of looking like a healthy one whose
        # tools fail.
        await alog_fallback(
            f"bridge_dead_{session.reason}", stub_uuid, pool_label, args
        )
        logger.warning(
            "bridge ended (%s) and could not re-attach after %d reconnect(s); "
            "this session loses these servers pool=%s",
            session.reason,
            session.reconnects,
            pool_label,
        )
        return 1
    return 0


def _hard_exit(code: int) -> NoReturn:
    """Terminate without interpreter finalization.

    ``sys.exit()`` cannot be used here. The ``stub-stdin`` daemon thread parks
    in ``sys.stdin.buffer.readline()`` for the life of the process, holding that
    stream's lock, and interpreter finalization tries to flush and close the
    same ``BufferedReader``. CPython cannot acquire the lock and aborts the
    process with ``Fatal Python error: _enter_buffered_busy`` (SIGABRT, exit
    134, core dumped) instead of returning ``code``.

    Finalization is skipped, so do its two jobs that matter here explicitly:
    ``logging.shutdown()`` drains handler buffers (the only handler is a
    ``StreamHandler`` on stderr, configured in :func:`_amain`), then stderr is
    flushed. stdout is deliberately NOT flushed here: every stdout frame is
    written and flushed by the dedicated ``stub-stdout`` daemon writer thread,
    which holds the buffer's lock while blocked on a stalled downstream pipe —
    flushing from this thread would deadlock on that lock and the process
    would never reach ``os._exit``. Nothing else is owed — the
    atexit-registered thread-pool joins in ``kiro_crew.executors`` hold no
    unflushed state, only threads the OS reclaims, and skipping them also
    drops a teardown hang risk on a wedged filesystem read.
    """
    try:
        logging.shutdown()
    except Exception:  # pragma: no cover — never block exit on log teardown
        pass
    try:
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    os._exit(code)


def main() -> None:
    """Sync entry point for ``python -m kiro_crew.mcp_gateway.stub``."""
    try:
        rc = asyncio.run(_amain())
    except KeyboardInterrupt:
        rc = 0
    _hard_exit(rc)


if __name__ == "__main__":
    main()
