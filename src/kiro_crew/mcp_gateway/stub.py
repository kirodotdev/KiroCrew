"""KiroCrew MCP stub — shim between kiro-cli and the gateway daemon.

The stub is the shim kiro-cli execs in place of the real MCP binary. It
connects to gatewayd over a unix socket, Registers with a full
:class:`PoolKey` payload, then bridges kiro-cli stdio ↔ gateway until
either side closes. On handshake failure it logs a structured fallback
record to ``$KIROCREW_HOME/logs/stub_fallback.jsonl`` and ``execvpe``\u200bs
the real MCP backend in place, preserving per-session correctness.

Register fields match :meth:`PoolKey.from_register`; hashes use SHA-256
(stdlib). Bridge phase is NOT wrapped in a timeout (learned correction
— a single timeout around a long-lived session silently kills healthy
streams). Import budget: stdlib + pool + mcp_caller only.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import queue
import shutil
import signal
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from kiro_crew import platform_compat
from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_caller import CallerContext, _parent_pid
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.hashing import hash_command, hash_effective_env
from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES, PoolKey

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
# Pre-flight ``ensure_backend`` reply timeout. Must comfortably
# exceed cold backend fork latency. On timeout the stub falls back to a
# direct per-session exec, so an over-generous value only costs a slower
# recovery when the gateway is genuinely wedged.
_ENSURE_BACKEND_TIMEOUT_SECS = 25.0
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
    # Accept legacy socket name written by older versions (#928).
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
    revisions."""
    p = argparse.ArgumentParser(
        prog="kirocrew-mcp-stub",
        description="KiroCrew MCP shim: proxies kiro-cli stdio to the local gateway",
    )
    p.add_argument("--server", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--target-command", required=True, dest="target_command")
    p.add_argument("--target-args", default="", dest="target_args")
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
    p.add_argument("--channel-id", default=None, dest="channel_id")
    p.add_argument(
        "--socket",
        default=os.environ.get("KIROCREW_MCP_SOCKET") or os.environ.get("MC_MCP_SOCKET") or _default_socket_path(),
    )
    p.add_argument("--real-stub", default=None, dest="real_stub")
    return p.parse_args(argv)


def _split_target_args(raw: str, sep: str) -> list[str]:
    return raw.split(sep) if raw else []


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
    except (json.JSONDecodeError, ValueError):
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
        #                  so ["a,b"] and ["a","b"] no longer collide onto one key.
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
    principal = (
        os.environ.get("KIROCREW_PRINCIPAL") or os.environ.get("USER") or ""
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
    target_args = _split_target_args(args.target_args, args.target_args_sep)
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

    try:
        work_dir = str(Path(args.work_dir).resolve())
    except OSError:
        work_dir = str(args.work_dir)

    caller = _build_caller_block(channel_id)
    # USERNAME is the Windows spelling of USER; check both so this diagnostic
    # dimension is not empty on one platform.
    user_identity = (
        caller["principal_id"]
        or os.environ.get("USER", "")
        or os.environ.get("USERNAME", "")
        or "unknown"
    )

    return {
        "type": "register",
        "stub_uuid": str(uuid.uuid4()),
        "server_name": args.server,
        "agent_name": args.agent,
        "command_args_hash": hash_command(args.target_command, target_args),
        "effective_env_hash": hash_effective_env(env_pairs),
        "work_dir": work_dir,
        "binary_version": _binary_version(args.target_command),
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
        "user_identity": user_identity,
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


class BridgeLivenessFailure:
    """Returned by :func:`run_bridge` when the peer stops responding to
    liveness pings while requests are outstanding. Contains the IDs of
    the requests that were still pending so the caller can emit JSON-RPC
    error frames before degrading."""

    __slots__ = ("outstanding_ids",)

    def __init__(self, outstanding_ids: list) -> None:  # noqa: D107
        self.outstanding_ids = outstanding_ids


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
) -> Optional[BridgeLivenessFailure]:
    """Pump stdin ↔ socket until either side closes or ``stop_event`` fires.

    ``stdin``/``stdout_writer`` are dependency-injected so tests can drive
    the bridge through in-process pipes; ``None`` falls back to real
    ``sys.stdin``/``sys.stdout``. On clean stdin EOF an ``Unregister``
    frame is sent so gatewayd detaches without waiting on refcount.

    Returns ``None`` on normal termination. Returns a
    :class:`BridgeLivenessFailure` when the gateway stopped answering
    liveness pings while requests were outstanding — the caller should
    emit JSON-RPC errors for the listed IDs and fall back to a direct
    exec.
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
        # Inbound line source. Tests inject an in-process StreamReader; the
        # real stub reads sys.stdin on a DEDICATED DAEMON THREAD and hands
        # lines over via a queue. A daemon thread is never joined at
        # interpreter/asyncio shutdown, so a readline still blocked because
        # kiro-cli holds stdin open can no longer hang graceful SIGTERM — the
        # old run_in_executor(readline) used the default executor, which
        # asyncio.run() joins via shutdown_default_executor().
        loop = asyncio.get_running_loop()
        # Bounded + backpressured: the reader thread blocks on put() (via
        # run_coroutine_threadsafe) when the queue is full, so a stalled
        # writer.drain() (gatewayd slow to accept) can no longer let the reader
        # keep draining stdin into an unbounded queue and balloon RSS.
        line_q: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=256)
        if stdin is None:
            def _blocking_reader() -> None:
                fh = sys.stdin.buffer
                try:
                    while True:
                        chunk = fh.readline()
                        # Block this thread until the queue has room. .result()
                        # waits on the loop; if the loop has stopped (shutdown)
                        # it raises and the daemon thread simply exits.
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

        async def _next_line() -> bytes:
            if stdin is not None:
                try:
                    return await stdin.readuntil(b"\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    return b""
            return await line_q.get()

        while True:
            line = await _next_line()
            if not line:
                try:
                    await _write_frame(writer, {"type": "unregister"})
                except Exception:
                    pass
                return
            # Track outbound JSON-RPC request IDs (have "method" + "id").
            # Best-effort: parse failures are silently ignored — the frame is
            # still forwarded verbatim.
            try:
                msg = json.loads(line)
                if isinstance(msg, dict) and "method" in msg and "id" in msg:
                    _outstanding_ids.add(msg["id"])
            except (json.JSONDecodeError, ValueError, TypeError):
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
                    return
                if not line:
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
                        elif "id" in msg and "method" not in msg:
                            # A response (has id, no method) — clear from
                            # outstanding set.
                            _outstanding_ids.discard(msg["id"])
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
                # Control frames are gateway<->stub only; never forward to
                # kiro-cli stdout.
                if _is_control:
                    continue
                try:
                    await _emit(line)
                except (ConnectionError, BrokenPipeError):
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

        Each ping/miss cycle consumes exactly ONE ``ping_interval``: when
        something is outstanding the wait for the pong *is* the interval, so the
        advertised grace is ``ping_interval × ping_max_misses`` rather than twice
        that. Only an idle bridge sleeps separately, and it resets the miss count
        so an earlier partial streak cannot carry across an idle gap.

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
            # This wait IS the cycle's interval — do not sleep again.
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
    result: Optional[BridgeLivenessFailure] = None
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _safe_close(writer)
        # If the peer-dead event fired, report a liveness failure so the
        # caller can emit JSON-RPC errors and degrade.
        if _peer_dead_evt.is_set() and _outstanding_ids:
            result = BridgeLivenessFailure(list(_outstanding_ids))
    return result


def _fallback_log_path() -> Path:
    return _crew_home() / "logs" / "stub_fallback.jsonl"


def log_fallback(
    reason: str, stub_uuid: str, pool_label: str, args: argparse.Namespace
) -> None:
    """Append one JSON record to the fallback audit log. OS errors are
    swallowed — logging failure must never block the exec that keeps
    kiro-cli working."""
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
            "server": args.server,
            "agent": args.agent,
            "channel_id": args.channel_id or "",
            "target_command": args.target_command,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        pass


def fallback_exec(args: argparse.Namespace) -> None:
    """Replace the current process with the real MCP backend. ``execvpe``
    never returns on success; a return raises so the caller surfaces a
    diagnostic."""
    target_args = _split_target_args(args.target_args, args.target_args_sep)
    argv = [args.target_command, *target_args]
    # Restore the server's declared env. The rewriter moves declared env
    # (which routinely holds tokens / API keys) into a 0600 sidecar the stub
    # otherwise reads only for PoolKey hashing. On this fallback path we exec
    # the real backend directly, so it must run with its declared env to match
    # the non-pooled baseline — the daemon's own environment lacks it.
    exec_env = dict(os.environ)
    exec_env.update(_parse_env_file(getattr(args, "env_file", "") or ""))
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
        log_fallback("handshake_timeout", payload["stub_uuid"], pool_label, args)
        logger.warning("handshake timed out; falling back pool=%s", pool_label)
        fallback_exec(args)
        return 1  # unreachable
    except FallbackRequestedError as exc:
        log_fallback(exc.reason, payload["stub_uuid"], pool_label, args)
        logger.warning("handshake failed (%s); falling back pool=%s", exc.reason, pool_label)
        fallback_exec(args)
        return 1  # unreachable
    logger.info("registered stub_uuid=%s pool=%s", stub_uuid, pool_label)

    # B1 pre-flight: trigger the gateway's backend spawn with a
    # control frame BEFORE forwarding any real MCP traffic, so a capacity /
    # breaker rejection reaches us while kiro-cli's ``initialize`` is still
    # unread in fd0 (a clean per-session exec fallback). Gated on the gateway
    # advertising the ``ensure_backend`` capability: an OLD gateway without it
    # would treat the control frame as a real MCP frame and never reply, so we
    # skip the pre-flight and bridge directly (legacy lazy-spawn path, no 25s
    # skew penalty).
    capabilities = registered.get("capabilities") if isinstance(registered, dict) else None
    if isinstance(capabilities, list) and "ensure_backend" in capabilities:
        try:
            await _write_frame(writer, {"type": "ensure_backend"})
            ready = await asyncio.wait_for(
                _read_frame(reader), timeout=_ENSURE_BACKEND_TIMEOUT_SECS
            )
        except (asyncio.TimeoutError, OSError, ConnectionError, json.JSONDecodeError) as exc:
            # Gateway unreachable / wedged mid-pre-flight — same posture as a
            # connect failure: fall back to a direct per-session exec.
            await _safe_close(writer)
            log_fallback(
                f"ensure_backend_io:{type(exc).__name__}",
                payload["stub_uuid"], pool_label, args,
            )
            logger.warning("ensure_backend io failed (%s); falling back pool=%s", exc, pool_label)
            fallback_exec(args)
            return 1  # unreachable
        if not (isinstance(ready, dict) and ready.get("type") == "ready"):
            if (
                isinstance(ready, dict)
                and ready.get("type") == "rejected"
                and not ready.get("fallback")
            ):
                # Terminal rejection (unknown target / genuine spawn failure):
                # the gateway says this server cannot run. Surface the failure
                # instead of exec'ing — matches the lazy path and avoids
                # per-session crash-loops of a broken backend.
                await _safe_close(writer)
                logger.error(
                    "gateway terminally rejected ensure_backend (%s); not falling back pool=%s",
                    ready.get("reason"), pool_label,
                )
                return 1
            # Fallback-eligible rejection (``fallback: true``) or a closed /
            # garbage reply -> exec the real backend directly for this session.
            reason = (
                ready.get("reason", "ensure_backend_rejected")
                if isinstance(ready, dict) else "ensure_backend_closed"
            )
            await _safe_close(writer)
            log_fallback(reason, payload["stub_uuid"], pool_label, args)
            logger.warning("gateway fallback-rejected ensure_backend (%s); falling back pool=%s", reason, pool_label)
            fallback_exec(args)
            return 1  # unreachable

    # Warm-pool caller repair: if we registered without a session key (the
    # kiro-cli was pool-spawned before its session was claimed), watch for the
    # key to materialize and re-register the caller so state-mutating tools
    # (learn_add et al.) work for the claimed session. No-op for stubs that
    # already had a key at register.
    recaller_task: Optional[asyncio.Task[None]] = None
    if not payload.get("session_key"):
        recaller_task = asyncio.create_task(
            _recaller_loop(writer, _resolve_channel_id(args.channel_id), stop_event),
            name="kirocrew-mcp-stub-recaller",
        )
    try:
        liveness_failure = await run_bridge(
            reader,
            writer,
            stop_event,
            peer_supports_ping=bool(
                isinstance(capabilities, list) and "bridge_ping" in capabilities
            ),
        )
    finally:
        if recaller_task is not None:
            if not recaller_task.done():
                recaller_task.cancel()
            # Always await — even a task that already finished (successfully or
            # with an exception) must have its result/exception retrieved, or
            # asyncio logs "Task exception was never retrieved" and hides a bug.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await recaller_task

    # Liveness failure: the gateway stopped answering pings while requests were
    # outstanding. Fail the outstanding calls FAST and CLEANLY instead of leaving
    # them parked forever.
    #
    # Deliberately NOT followed by fallback_exec. The four pre-flight fallback
    # sites work because kiro-cli's ``initialize`` is still unread in fd0, so the
    # exec'd server comes up initialized. Here stdin_pump has already consumed
    # and forwarded ``initialize``, and kiro-cli never re-sends it (see
    # gatewayd's ``captured_init`` rationale), so an exec'd server would be a
    # fresh, never-initialized MCP server that rejects every subsequent call.
    # Exec would convert "wedged" into "fast-failing", not into "working" — and
    # it would also throw away the socket a future reconnect could reuse.
    if liveness_failure is not None:
        def _emit_errors() -> None:
            """Write the error frames on a worker thread.

            A blocked kiro-cli reader (full stdout pipe) must not wedge the very
            path whose job is to unwedge the caller, so the write is offloaded
            and bounded by the caller's timeout rather than run inline.
            """
            for req_id in liveness_failure.outstanding_ids:
                err_frame = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32603,
                        "message": (
                            "Gateway stopped responding; this call was failed so "
                            "it would not hang. Retry it."
                        ),
                    },
                }
                sys.stdout.buffer.write(
                    json.dumps(err_frame, separators=(",", ":")).encode("utf-8") + b"\n"
                )
            sys.stdout.buffer.flush()

        try:
            await asyncio.wait_for(
                asyncio.to_thread(_emit_errors), timeout=_ERROR_EMIT_TIMEOUT_SECS
            )
        except (asyncio.TimeoutError, OSError, ValueError):
            # Nothing better to do: the reader is gone or wedged, and the caller
            # is already being abandoned. Exiting still closes stdout, which is
            # what tells kiro-cli this server is done.
            logger.warning("could not emit liveness error frames pool=%s", pool_label)
        log_fallback("bridge_liveness_dead", stub_uuid, pool_label, args)
        logger.warning(
            "bridge peer stopped answering pings; failed %d outstanding call(s) "
            "pool=%s",
            len(liveness_failure.outstanding_ids),
            pool_label,
        )
        return 1
    return 0


def main() -> None:
    """Sync entry point for ``python -m kiro_crew.mcp_gateway.stub``."""
    try:
        rc = asyncio.run(_amain())
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    main()
