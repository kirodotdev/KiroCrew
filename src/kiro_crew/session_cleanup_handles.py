"""The Windows handle-leak warning the session cleanup sweep runs every tick.

A process that opens one handle per directory entry stays small in every memory
metric while its handle table grows until the host runs out, so the memory
ceiling cannot see it. This pass reads the busiest handle count of every live
runtime's validated tree and logs one WARNING per session per window above
``WINDOWS_HANDLE_WARN_THRESHOLD``. It never recycles anything, so unlike the
memory ceiling it also covers the persistent runtimes, channel sessions,
sessions with a turn in flight, and the runtimes no session entry holds (the
shared background runtime and sub-agent runtimes, draining ones included), and
it runs whether or not ``session.watchdog_rss_max_mb`` is set.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from kiro_crew.constants import WINDOWS_HANDLE_WARN_THRESHOLD

if TYPE_CHECKING:
    from kiro_crew.session_cleanup import SessionCleanup, SessionEntry

    #: (session key, runtime pid, session, whether the memory ceiling may recycle it)
    LiveRuntime = tuple[str, int, SessionEntry, bool]


async def live_runtimes(
    cleanup: SessionCleanup, *, include_unrecyclable: bool
) -> list[LiveRuntime]:
    """Every session with a live runtime pid, read once for both tick passes.

    Persistent, channel and busy sessions are never recycled, so they are left
    out unless *include_unrecyclable*: the handle warning recycles nothing and
    still wants them.
    """
    persistent_keys = cleanup._deps.get_persistent_keys()
    channel_prefix = cleanup._deps.get_channel_prefix()
    runtimes: list[LiveRuntime] = []
    async with cleanup._owner._lock:
        for key, session in cleanup._owner._sessions.items():
            recyclable = not (
                key in persistent_keys
                or key.startswith(channel_prefix)
                or session.semaphore.locked()
            )
            if not recyclable and not include_unrecyclable:
                continue
            pid = cleanup._owner.get_pid(key)
            if pid is not None:
                runtimes.append((key, pid, session, recyclable))
    return runtimes


async def warn_handle_leaks(
    cleanup: SessionCleanup, runtimes: list[LiveRuntime], window_secs: float
) -> None:
    """Warn once per *window_secs* for each session whose tree leaks handles.

    The count belongs to a runtime, so co-tenants on one pid share one probe per
    tick; each of them is still named, because the operator acts on a session.
    The runtimes no session entry holds (the shared background runtime, sub-agent
    runtimes, and either kind while it drains) are probed too, named by pid.
    """
    state = cleanup.state
    deps = cleanup._deps
    now = deps.monotonic()
    for stale, at in list(state.handle_warned.items()):
        if now - at > window_secs:
            del state.handle_warned[stale]

    session_pids = {pid for _key, pid, _session, _ok in runtimes}
    targets = [(key, pid, f"Session {key}") for key, pid, _session, _ok in runtimes]
    targets += [
        (f"companion:{pid}", pid, "A companion runtime (background or sub-agent)")
        for pid in sorted(cleanup._owner._companion_runtime_pids() - session_pids)
    ]
    candidates = [target for target in targets if target[0] not in state.handle_warned]
    if not candidates:
        return

    loop = asyncio.get_running_loop()
    handles_by_pid: dict[int, int | None] = {}
    for key, pid, subject in candidates:
        if pid not in handles_by_pid:
            handles_by_pid[pid] = await loop.run_in_executor(
                deps.get_maintenance_executor(),
                deps.get_session_handle_peak,
                pid,
            )
        handles = handles_by_pid[pid]
        if handles is None or handles <= WINDOWS_HANDLE_WARN_THRESHOLD:
            continue
        state.handle_warned[key] = now
        deps.logger.warning(
            "%s tree holds %d handles on pid %d, above the "
            "%d-handle warning threshold; a process leaking handles can "
            "exhaust a Windows host while its working set stays small",
            subject,
            handles,
            pid,
            WINDOWS_HANDLE_WARN_THRESHOLD,
        )
