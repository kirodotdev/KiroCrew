"""What the daemon reports about itself: backend-acquire metrics and zombie post-mortems.

Telemetry is best-effort and never breaks the hot path; the zombie diagnostic
samples the accept loop every ``_ZOMBIE_PROBE_INTERVAL_SECS`` and, when it has died
silently, dumps every task's stack and ends the daemon so the watchdog respawns it.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.daemon import logger
from kiro_crew.mcp_gateway.pool import BackendPool

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway import gatewayd as facade
else:
    from kiro_crew.mcp_gateway.daemon import facade


def _emit_backend_acquire_metric(acquire_ms: float, *, warm: bool) -> None:
    """Emit kirocrew.mcp.backend.acquire.duration (best-effort).

    Shared by the ensure_backend + lazy-spawn paths and their unit tests so the
    metric name / attrs live in production, not duplicated in the test
    (tests must drive real production code).
    """
    try:
        facade.get_recorder().histogram(
            "kirocrew.mcp.backend.acquire.duration",
            acquire_ms,
            unit="ms",
            attrs={"warm": warm},
        )
    except Exception:  # telemetry must never break the gateway hot path
        logger.debug("backend.acquire metric emit failed", exc_info=True)


def _emit_lazy_load_metrics(elapsed_ms: float, *, warm: bool) -> None:
    """Emit MCP lazy-load count + duration (+ backend.acquire), best-effort.

    Shared by the lazy-spawn path and its unit test.
    """
    try:
        rec = facade.get_recorder()
        rec.counter("kirocrew.mcp.lazy_load.count", attrs={"transport": "stdio"})
        rec.histogram(
            "kirocrew.mcp.lazy_load.duration",
            elapsed_ms,
            unit="ms",
            attrs={"transport": "stdio"},
        )
    except Exception:  # telemetry must never break the gateway hot path
        logger.debug("lazy_load metric emit failed", exc_info=True)
    _emit_backend_acquire_metric(elapsed_ms, warm=warm)


# --- Zombie diagnostic ------------------------------------------------------

# Chronic post-M5 issue: gatewayd's accept coroutine has been observed to
# exit silently every ~2-3 h on the dev soak. The existing heartbeat only
# proves the heartbeat task itself is alive; it does not prove the server
# is still accepting connections. The diagnostic task below closes that
# gap: it polls ``server.is_serving()`` and, on divergence from the
# expected "serving while stop_event unset" invariant, dumps a full
# post-mortem to a JSONL side-channel so the next event has a root-cause
# paper trail.

# Interval between diagnostic snapshots. A 30 s sample rate catches the
# ~90 s window between zombie death and watchdog kill without generating
# excessive log volume in the healthy case.
_ZOMBIE_PROBE_INTERVAL_SECS = 30.0


def _zombie_diagnostic_path() -> Path:
    """Return the JSONL file path that receives zombie post-mortems.

    Lives next to the soak/gatewayd logs under
    ``$KIROCREW_HOME/logs/gatewayd_zombie_diagnostic.jsonl`` so a single
    ``tail -f`` follows both heartbeat (gatewayd.log) and any detected
    zombie state.
    """
    return facade._config_dir() / "logs" / "gatewayd_zombie_diagnostic.jsonl"


def _count_open_fds() -> int:
    """Return the number of open file descriptors (or handles on Windows).

    FD exhaustion is one of the four hypothesised zombie causes; tracking
    the count per snapshot lets us confirm or eliminate that path without
    deploying a separate tracer.

    Delegates to :func:`platform_compat.count_open_fds` — the one shared
    per-platform probe (Linux ``/proc/self/fd``, macOS/BSD ``/dev/fd``,
    Windows ``GetProcessHandleCount``), also behind the
    ``kirocrew.process.open_fds`` gauge — so this diagnostic cannot drift
    from the figure the metrics report. The shared probe subtracts the
    enumeration fd on POSIX, so the value here is exactly one lower than the
    raw count the pre-consolidation duplicate reported; immaterial for a
    zombie-diagnostic snapshot field.

    Returns ``-1`` when the platform cannot provide the value.
    """
    count = facade._shared_count_open_fds()
    return -1 if count is None else count


def _read_rss_kb() -> int:
    """Return this process's CURRENT RSS in kilobytes, or ``-1`` if unavailable.

    Delegates to :func:`platform_compat.proc_rss_bytes` — the one per-platform
    current-RSS reader — so this diagnostic cannot drift from the figure the
    dashboard reports. A separate per-platform reader here would reach for
    ``ru_maxrss`` on macOS, which is a high-water mark that never decreases, so
    a spike the gateway had already released would stay in every later snapshot.
    """
    rss_bytes = facade._proc_rss_bytes()
    return rss_bytes // 1024 if rss_bytes > 0 else -1


def _collect_task_stacks() -> list[dict[str, Any]]:
    """Snapshot every live asyncio task with name + current stack.

    Used on zombie detection — gives the post-mortem enough context to
    tell whether a specific coroutine (backend pump, stub handler, idle
    sweeper) wedged the event loop versus an external cause (FD leak,
    blocking syscall, etc.).
    """
    out: list[dict[str, Any]] = []
    for task in asyncio.all_tasks():
        frames: list[str] = []
        try:
            for frame in task.get_stack(limit=10):
                frames.append(
                    "{}:{} in {}".format(
                        frame.f_code.co_filename,
                        frame.f_lineno,
                        frame.f_code.co_name,
                    )
                )
        except Exception:  # pragma: no cover — defensive
            frames = ["<stack unavailable>"]
        out.append(
            {
                "name": task.get_name(),
                "done": task.done(),
                "cancelled": task.cancelled(),
                "stack": frames,
            }
        )
    return out


def _snapshot_state(
    *,
    server: Optional[transport.TransportServer],
    pool: BackendPool,
    connections: set[asyncio.Task[None]],
    task_count: int,
) -> dict[str, Any]:
    """Gather a single health sample used by the diagnostic loop."""
    is_serving: Optional[bool]
    try:
        is_serving = bool(server.is_serving()) if server is not None else None
    except Exception:
        is_serving = None
    return {
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ts_epoch": time.time(),
        "pid": os.getpid(),
        "is_serving": is_serving,
        "task_count": task_count,
        "fd_count": _count_open_fds(),
        "rss_kb": _read_rss_kb(),
        "pool_size": len(pool._backends),  # type: ignore[attr-defined]
        "connections_in_flight": len(connections),
    }


def _write_diagnostic(path: Path, *records: dict[str, Any]) -> None:
    """Append one JSONL line per record to the diagnostic side-channel.

    Records that belong to the same event MUST be passed in a single call:
    they share one open-append-close cycle. Back-to-back appends from
    separate calls can collide on Windows — an open that lands while the
    previous writer's handle is still closing fails with a sharing
    violation — and the never-raises contract below turns that transient
    collision into a silently dropped record.

    Never raises — the diagnostic task is defensive enough that a missing
    directory or EROFS on the log volume must not crash gatewayd itself.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as exc:  # pragma: no cover — defensive
        logger.warning("zombie diagnostic write failed: %s", exc)


async def _zombie_diagnostic(
    server: transport.TransportServer,
    pool: BackendPool,
    connections: set[asyncio.Task[None]],
    stop_event: asyncio.Event,
) -> None:
    """Polling watchdog that captures accept-loop death.

    Every :data:`_ZOMBIE_PROBE_INTERVAL_SECS` seconds:

    1. Collect a health snapshot via :func:`_snapshot_state`, tagged
       ``probe`` — the continuous baseline to correlate against.
    2. If ``server.is_serving()`` is ``False`` while ``stop_event`` is
       still unset, the accept loop has died silently — append the probe
       baseline and a ``zombie_detected`` dump of every live task stack
       through a single write, log at error level, and set ``stop_event``
       so the process exits cleanly and the watchdog respawns us.
    3. Otherwise append just the probe baseline.
    """
    diag_path = facade._zombie_diagnostic_path()
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=facade._ZOMBIE_PROBE_INTERVAL_SECS
                )
                return  # stop_event fired — clean exit
            except asyncio.TimeoutError:
                pass

            # asyncio.all_tasks() must be read ON the loop (it needs the
            # running loop); capture it here before offloading the blocking
            # /proc walk — calling it inside the worker thread raises
            # RuntimeError and would kill this watchdog on its first probe.
            task_count = len(asyncio.all_tasks())
            snap = await asyncio.to_thread(
                _snapshot_state,
                server=server,
                pool=pool,
                connections=connections,
                task_count=task_count,
            )
            snap["tag"] = "probe"

            if snap["is_serving"] is False and not stop_event.is_set():
                # The accept loop died silently. The probe baseline and the
                # zombie dump go through ONE _write_diagnostic call (a single
                # open) — two back-to-back appends race on Windows, where the
                # second open can hit a sharing violation while the first
                # writer's handle is still closing, silently dropping the
                # zombie_detected record.
                dump = dict(snap)
                dump["tag"] = "zombie_detected"
                dump["tasks"] = _collect_task_stacks()
                dump["traceback"] = traceback.format_stack()
                await asyncio.to_thread(facade._write_diagnostic, diag_path, snap, dump)
                logger.error(
                    "zombie gatewayd detected: is_serving=False while stop_event unset; "
                    "tasks=%d fd=%d rss_kb=%d — diagnostic dumped to %s; setting stop_event",
                    dump["task_count"],
                    dump["fd_count"],
                    dump["rss_kb"],
                    diag_path,
                )
                stop_event.set()
                return

            await asyncio.to_thread(facade._write_diagnostic, diag_path, snap)
    except asyncio.CancelledError:
        pass
