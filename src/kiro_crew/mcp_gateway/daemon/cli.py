"""The daemon's command line: argument parser, CLI socket default, async entry.

``gatewayd.main`` stays in the facade (it is the process's one ``asyncio.run``);
:func:`_amain` parses argv, names the default executor, installs the loop
exception handler, heartbeat and signal handlers, fills the sandbox probe cache
and runs ``gatewayd.run_gatewayd`` until it returns.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from kiro_crew.executors import configure_default_executor
from kiro_crew.mcp_gateway.admission import DEFAULT_CAPACITY, DEFAULT_CEILING, DEFAULT_FLOOR
from kiro_crew.mcp_gateway.daemon import logger
from kiro_crew.mcp_gateway.host_budget import resolve_limits

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway import gatewayd as facade
else:
    from kiro_crew.mcp_gateway.daemon import facade


# Subdirectory under ``$XDG_RUNTIME_DIR`` (or ``/tmp`` fallback) where the
# gateway puts its socket by default. Callers normally supply an explicit
# path via :func:`run_gatewayd`; this default is for tests and ad-hoc runs.
_DEFAULT_SOCKET_SUBDIR = "kirocrew"
_DEFAULT_SOCKET_NAME = "mcp-gateway.sock"


def _default_cli_socket_path() -> Path:
    """Fallback socket path for the CLI's ``--socket`` argparse default.

    This is used ONLY when ``python -m kiro_crew.mcp_gateway.gatewayd`` is
    invoked without an explicit ``--socket`` flag — a rare operator path,
    typically ad-hoc debugging. The Kiro Crew production path always
    derives the socket from ``McpGatewayConfig.socket_path`` / the
    ``default_socket_path()`` in :mod:`kiro_crew.mcp_gateway.rewriter`,
    which returns ``$KIROCREW_HOME/mcp-gateway/gateway.sock``.

    Preference order for this CLI fallback:
    1. ``$XDG_RUNTIME_DIR/kirocrew/mcp-gateway.sock`` when XDG is set.
    2. ``$KIROCREW_HOME``/config-dir ``/mcp-gateway/mcp-gateway.sock``.

    There is deliberately no ``/tmp`` tier. ``XDG_RUNTIME_DIR`` is unset on
    Windows, so a ``/tmp`` fallback would resolve against the current drive and
    have the daemon create a stray ``C:\\tmp`` for its lock file. The data-home
    tier is correct on every platform and matches where production puts the
    endpoint, so the CLI default and the production default now agree on
    everything but the leaf filename.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / _DEFAULT_SOCKET_SUBDIR / _DEFAULT_SOCKET_NAME
    home = os.environ.get("KIROCREW_HOME")
    base = Path(home) if home else facade._config_dir()
    return base / "mcp-gateway" / _DEFAULT_SOCKET_NAME


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mc-mcp-gatewayd",
        description="KiroCrew MCP gateway daemon — pools MCP backends across sessions",  # brand-ok
    )
    p.add_argument(
        "--socket",
        dest="socket",
        default=str(_default_cli_socket_path()),
        help="Unix socket path to bind. Default: $XDG_RUNTIME_DIR/kirocrew/mcp-gateway.sock",
    )
    p.add_argument(
        "--max-backends",
        dest="max_backends",
        type=int,
        default=20,
        help="Maximum concurrent backend subprocesses. LRU-evicted beyond this.",
    )
    p.add_argument(
        "--idle-timeout-secs",
        dest="idle_timeout_secs",
        type=int,
        default=300,
        help="Seconds an unattached POOLED BACKEND is kept before the idle "
        "sweeper evicts it. Bounds pool-entry lifetime only, never the "
        "daemon's own — the daemon exits on SIGTERM/SIGINT or when its own "
        "socket path disappears.",
    )
    p.add_argument(
        "--prewarm-count",
        dest="prewarm_count",
        type=int,
        default=0,
        help="Number of hottest observed (agent x server x channel) backends to "
        "spawn at startup, before the first stub connects, to remove the "
        "cold-after-restart new-chat latency. 0 (default) disables prewarming.",
    )
    p.add_argument(
        "--credential-watch-path",
        dest="credential_watch_paths",
        action="append",
        default=[],
        metavar="PATH",
        help="Credential file to watch for content changes (repeatable). On a "
        "real rotation, all pooled backends are drained via a blue-green "
        "cutover and respawned with the fresh credential. No flag "
        "(default) disables the watcher entirely.",
    )
    p.add_argument(
        "--owner-pid",
        dest="owner_pid",
        type=int,
        default=0,
        help="PID of the gateway process this daemon serves. When set, the daemon "
        "exits gracefully once that process is gone (checked with its start time, "
        "so a recycled PID does not count), instead of lingering to be adopted by "
        "a later gateway that may run different code. 0 (default) disables it.",
    )
    p.add_argument(
        "--spawn-concurrency",
        dest="spawn_concurrency",
        type=int,
        default=DEFAULT_CAPACITY,
        help="Daemon-wide number of backend spawn+initialize windows in flight at "
        "once; further spawns wait FIFO. Clamped to [--spawn-concurrency-min, "
        "--spawn-concurrency-max].",
    )
    p.add_argument(
        "--spawn-concurrency-min",
        dest="spawn_concurrency_min",
        type=int,
        default=DEFAULT_FLOOR,
        help="Floor the adaptive controller may lower the spawn concurrency to.",
    )
    p.add_argument(
        "--spawn-concurrency-max",
        dest="spawn_concurrency_max",
        type=int,
        default=DEFAULT_CEILING,
        help="Ceiling the adaptive controller may raise the spawn concurrency to.",
    )
    p.add_argument(
        "--spawn-queue-wait-secs",
        dest="spawn_queue_wait_secs",
        type=float,
        default=600.0,
        help="Longest a queue-aware stub is held in the spawn gate before a " "capacity rejection.",
    )
    p.add_argument(
        "--initialize-timeout-secs",
        dest="initialize_timeout_secs",
        type=float,
        default=10.0,
        help="Bound on a backend's first MCP initialize; also the spawn-gate "
        "permit window after ready.",
    )
    p.add_argument(
        "--host-budget-max-procs",
        dest="host_budget_max_procs",
        type=int,
        default=0,
        help="Ceiling on backend processes this daemon is answerable for (pooled, "
        "private and fallback alike). 0 derives it from --host-available-mb and "
        "--max-backends.",
    )
    p.add_argument(
        "--host-budget-max-rss-mb",
        dest="host_budget_max_rss_mb",
        type=int,
        default=0,
        help="Ceiling on the summed per-backend RSS estimate. 0 = unbounded.",
    )
    p.add_argument(
        "--host-budget-max-fds",
        dest="host_budget_max_fds",
        type=int,
        default=0,
        help="Ceiling on the daemon's own descriptors held for backends (3 per "
        "process). 0 derives it from the process's RLIMIT_NOFILE soft limit.",
    )
    p.add_argument(
        "--host-available-mb",
        dest="host_available_mb",
        type=float,
        default=-1.0,
        help="Available host memory (MiB) sampled by the supervising gateway, used "
        "only to derive an automatic process ceiling. Negative = unknown.",
    )
    p.add_argument(
        "--log-level",
        dest="log_level",
        default=os.environ.get("MC_GATEWAYD_LOG", "INFO"),
        help="Python logging level (DEBUG, INFO, WARNING, ...).",
    )
    return p


async def _amain(argv: Optional[list[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    # ── Name the default executor ──
    # asyncio.to_thread and run_in_executor(None, ...) route onto the loop's
    # default executor, which Python names threads anonymously.  This names
    # them ``mc-default`` so profilers like py-spy can attribute blocking work
    # to this gateway.  Must run BEFORE any to_thread offload.
    configure_default_executor()

    # Catch exceptions that slip past per-task handlers — e.g. a
    # fire-and-forget coroutine that blows up without ``await``. Without
    # this hook they get logged through asyncio's default handler only
    # if the task is awaited; zombie modes have been traced to exactly
    # this path.
    def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        msg = context.get("message", "unhandled event loop error")
        if exc is not None:
            logger.error("gatewayd event-loop exception: %s", msg, exc_info=exc)
        else:
            logger.error("gatewayd event-loop error: %s | context=%r", msg, context)

    loop.set_exception_handler(_loop_exception_handler)

    # Heartbeat: emit a line every 60s so a silent stdout stream becomes
    # visible proof that the daemon has zombified. Also logs pool stats
    # to give shape to load growth between heartbeats.
    async def _heartbeat() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                logger.info("gatewayd heartbeat: alive, stop_event=unset")
            except asyncio.CancelledError:
                return

    hb_task = asyncio.create_task(_heartbeat(), name="mcp-gateway-heartbeat")

    # Fill the sandbox probe cache BEFORE any backend is spawned on-loop. See the
    # matching site in slack/gateway.py: the wait is what makes the guarantee
    # hold, since a fire-and-forget prewarm leaves the first spawn racing the
    # warm thread and reading a cold-cache transient as "no sandbox backend".
    try:
        await asyncio.to_thread(facade.warm_backend)
    except RuntimeError:
        logger.warning("sandbox warm_backend skipped (thread exhaustion); cache stays cold")

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await facade.run_gatewayd(
            args.socket,
            max_backends=args.max_backends,
            idle_timeout_secs=args.idle_timeout_secs,
            stop_event=stop_event,
            prewarm_count=args.prewarm_count,
            credential_watch_paths=[Path(p) for p in args.credential_watch_paths],
            owner_pid=max(0, int(args.owner_pid or 0)),
            spawn_concurrency=args.spawn_concurrency,
            spawn_concurrency_min=args.spawn_concurrency_min,
            spawn_concurrency_max=args.spawn_concurrency_max,
            spawn_queue_wait_secs=max(1.0, float(args.spawn_queue_wait_secs)),
            initialize_timeout_secs=max(1.0, float(args.initialize_timeout_secs)),
            host_budget_limits=resolve_limits(
                max_procs=max(0, int(args.host_budget_max_procs)),
                max_rss_mb=max(0, int(args.host_budget_max_rss_mb)),
                max_fds=max(0, int(args.host_budget_max_fds)),
                available_mb=(
                    float(args.host_available_mb) if args.host_available_mb >= 0 else None
                ),
                max_backends=args.max_backends,
            ),
        )
    except Exception:
        logger.exception("gatewayd exited with unhandled exception")
        return 1
    finally:
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await hb_task
    return 0
