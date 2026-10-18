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
streams). Import budget: stdlib, ``transport`` and the stdlib-only leaves it
needs to connect. The stub runs once per session per MCP server, so anything
else -- pool, config, metrics, the caller and cleanup helpers -- is imported by
the function that uses it (see :data:`_DEFERRED`), and
``test_mcp_gateway_stub_import_budget.py`` holds the line.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib as _importlib
import logging
import os
import signal
import sys
import sys as _sys
from typing import TYPE_CHECKING, NoReturn, Optional

from kiro_crew import _facade_mirror
from kiro_crew.executors import configure_default_executor, subprocess_executor
from kiro_crew.mcp_gateway.hashing import expand_stub_flags, format_pool_label
from kiro_crew.mcp_gateway.stub_bridge import (
    StubSession,
    _emit_error_frames,
    _safe_close,
    _write_frame,
    logger,
    run_bridge,
)
from kiro_crew.mcp_gateway.stub_fallback import (
    _keep_socket_across_exec,
    alog_fallback,
    fallback_exec,
)
from kiro_crew.mcp_gateway.stub_link import (
    _ADMIT_CLOSED,
    _ADMIT_REJECTED,
    _ADMIT_TIMEOUT,
    _FALLBACK_CLASSES,
    _HANDSHAKE_TIMEOUT_SECS,
    _REJECT_CLASS_CAPACITY,
    _SPAWN_QUEUE_WAIT_BUDGET_SECS,
    FallbackRequestedError,
    _adopt_read_ceiling,
    _ensure_backend_admitted,
    _reconnect_while_draining,
    _rejection_class,
    _serve_capacity_refusal,
    _split_abandoned_ids,
    handshake,
    must_degrade_nonce_blind,
    must_degrade_unknown_target,
    must_degrade_unshareable,
)
from kiro_crew.mcp_gateway.stub_register import (
    _build_caller_block,
    _default_socket_path,
    _resolve_channel_id,
    build_register_payload,
)

# --- Import budget ----------------------------------------------------------
# The stub runs once per session per MCP server, so what it loads is multiplied
# by every session on the host. Two rules keep that bounded, and they are
# different rules because they bound different things:
#
# * The modules ABOVE are the ones every run reaches anyway -- it builds a
#   Register payload and connects on every start -- so importing them here
#   rather than inside a function changes when, not whether, and the eager
#   spelling is the readable one.
# * ``mcp_gateway.pool``, ``metrics.events`` and ``jsonl_util`` are NOT on that
#   path, and ``pool`` in particular costs the whole config package (its body
#   resolves the read-buffer ceiling, which reads config). They are imported by
#   the function that needs them: a reconnect counter, a fallback record, a
#   degrade check. ``test_mcp_gateway_stub_import_budget.py`` holds that line.
#
# The ceiling the stub DOES need comes from the stdlib-only ``read_limits``
# leaf, and the pool label it logs comes from ``hashing.format_pool_label``, so neither
# requires ``pool`` itself.


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
    # The read ceiling, pre-resolved from config by the rewriter that wrote this
    # overlay. Taking it on argv is what keeps a running stub out of the config
    # package: see :func:`_adopt_read_ceiling`. Absent on an overlay written
    # before the flag existed, which the resolver handles. ``type=str`` and not
    # ``int``: an unparsable value must degrade to the resolver, and argparse's
    # own int conversion would exit the process instead.
    p.add_argument("--read-limit", default=None, dest="read_limit")
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# Compatibility facade. The stub's machinery lives in the ``stub_*`` owner modules
# listed below, one responsibility each; this module keeps the process: its argv,
# its signal handlers, the warm-pool caller repair and :func:`_amain`, which drives
# the owners from first connect to exit. Every name stays readable as
# ``kiro_crew.mcp_gateway.stub.<name>``: this module answers a name it does not
# bind from the owner that defines it, and a write or delete through this module
# reaches every owner binding the same object (``kiro_crew._facade_mirror``), so a
# patch of ``kiro_crew.mcp_gateway.stub.<name>`` reaches every reader it reached
# when this was one module. Owners never import this module. The install runs
# here, above the process code, which binds its own names on this module after
# it, and reads ``_sys.modules[__name__]``: under ``python -m`` this module is
# ``__main__``.
# --------------------------------------------------------------------------- #
#: The owner modules, lowest layer first: an owner imports only owners listed before
#: it, and the first holder of a name answers a read of it.
_STUB_OWNERS: tuple[str, ...] = (
    "kiro_crew.mcp_gateway.stub_bridge",
    "kiro_crew.mcp_gateway.stub_register",
    "kiro_crew.mcp_gateway.stub_fallback",
    "kiro_crew.mcp_gateway.stub_link",
)
for _owner in _STUB_OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(
    _facade_mirror.install(_sys.modules[__name__], _STUB_OWNERS, excluded=()).served
)
# The forwarding is live from here for every caller. The definitions below bind
# their names on this module directly, and a write through it of a name it binds
# itself stays here (``_facade_mirror._creation_owners``).
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    # The names the unsplit module imported from the rest of kiro_crew and that only
    # an owner reads now, from their home modules.
    from kiro_crew import platform_compat  # noqa: F401
    from kiro_crew.json_line import parse_json_object_line  # noqa: F401
    from kiro_crew.mcp_caller import (  # noqa: F401
        CallerContext,
        _parent_pid,
    )
    from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS  # noqa: F401
    from kiro_crew.mcp_gateway import transport  # noqa: F401
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV  # noqa: F401
    from kiro_crew.mcp_gateway.hashing import (  # noqa: F401
        decode_target_args,
        hash_command,
        hash_effective_env,
        runs_install_code,
    )
    from kiro_crew.mcp_gateway.read_limits import (  # noqa: F401
        _DEFAULT_READ_BUFFER_LIMIT,
        read_buffer_limit_from_flag,
    )
    from kiro_crew.mcp_gateway.shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS  # noqa: F401
    from kiro_crew.mcp_gateway.stub_bridge import (  # noqa: F401
        _BRIDGE_KEEPALIVE_TYPE,
        _BRIDGE_PING_INTERVAL_SECS,
        _BRIDGE_PING_MAX_MISSES,
        _BRIDGE_PING_TYPE,
        _BRIDGE_PONG_TYPE,
        _ERROR_EMIT_TIMEOUT_SECS,
        _SPAWN_QUEUED_TYPE,
        _STDIN_QUEUE_MAXSIZE,
        _is_json_non_object,
        _NonObjectFrame,
        _read_frame,
    )
    from kiro_crew.mcp_gateway.stub_fallback import (  # noqa: F401
        _EXIT_UNRESOLVED_SECRET,
        _FALLBACK_COUNT_WINDOW_SECS,
        _FALLBACK_LOG_MAX_BYTES,
        _child_exit_status,
        _fallback_log_path,
        _fallback_spawn_child,
        _fallback_target_is_own_control_plane,
        _fallback_user_site_holds_our_package,
        _inherited_std_fd,
        _refuse_unresolved_secret_references,
        fallback_counts,
        log_fallback,
    )
    from kiro_crew.mcp_gateway.stub_link import (  # noqa: F401
        _ADMIT_READY,
        _CAPACITY_ERROR_CODE,
        _ENSURE_BACKEND_TIMEOUT_SECS,
        _HELD_FRAME_BYTES,
        _HELD_TOTAL_BYTES,
        _QUEUED_FAIL_MESSAGE,
        _QUEUED_GRACE_SECS,
        _RECONNECT_BACKOFF_MAX_SECS,
        _RECONNECT_BACKOFF_START_SECS,
        _RECONNECT_CRASH_LOOP_CYCLES,
        _RECONNECT_TOTAL_BUDGET_SECS,
        _REJECT_CLASS_COMPAT,
        _REJECT_CLASS_ISOLATION,
        _REPLAY_INIT_TIMEOUT_SECS,
        _REPLAY_OK,
        _REPLAY_REFUSE,
        _REPLAY_RETRY,
        _SPAWN_QUEUE_SILENCE_SECS,
        _SUPERVISOR_COLD_START_SECS,
        _SUPERVISOR_LIVENESS_MAX_FAILURES,
        _SUPERVISOR_LIVENESS_PING_INTERVAL_SECS,
        _SUPERVISOR_RESPAWN_BACKOFF_MAX_SECS,
        _SUPERVISOR_RESPAWN_CYCLE_SECS,
        READ_BUFFER_LIMIT_BYTES,
        TARGET_UNKNOWN_REASON,
        StaleGenerationError,
        _admission_control_frame,
        _drain_while_disconnected,
        _is_failable_request,
        _no_room_to_hold,
        _reconnect,
        _reconnect_now,
        _reconnect_wait,
        _replay_initialize,
        _write_stdout_line,
    )
    from kiro_crew.mcp_gateway.stub_register import (  # noqa: F401
        _ANCESTOR_WALK_MAX,
        _BINARY_HASH_CAP_BYTES,
        _CONFIG_SNAPSHOT_PLACEHOLDER,
        _KIROCREW_MCP_SUBCOMMANDS,
        _ancestor_pids,
        _binary_version,
        _crew_home,
        _hash_permission_profile,
        _parse_auto_approve,
        _parse_env_csv,
        _parse_env_file,
        _parse_env_json,
        _pool_binary_identity,
        _resolve_pool_identity_env,
        _resolve_target_args,
        _split_target_args,
        binary_fingerprint,
        pool_binary_version,
    )


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
    _adopt_read_ceiling(args)
    # build_register_payload -> _build_caller_block -> CallerContext.from_env
    # does a synchronous /proc ancestry walk + file reads (and _binary_version
    # hashes the target binary), so offload the whole cold-start resolution to
    # the dedicated subprocess pool (not the shared default) — consistent with
    # _recaller_loop, so a wedged filesystem read can't starve default-pool work.
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(subprocess_executor(), build_register_payload, args)

    try:
        pool_label = format_pool_label(payload)
    except ValueError as exc:
        # Defensive — our own payload should never be malformed.
        logger.warning("built an unlabelable Register payload: %s", exc)
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


#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(
    name
    for name in set(globals()) | set(_EXPORTS)
    if not name.startswith("_") and name != "TYPE_CHECKING"
)


if __name__ == "__main__":
    main()
