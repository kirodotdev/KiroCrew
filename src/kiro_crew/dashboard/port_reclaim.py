"""Reclaim the dashboard bind port from a *stale* KiroCrew gateway.

Background
----------
The gateway binds its dashboard port once at startup (:func:`_start_site` in
:mod:`kiro_crew.dashboard.server`). If the port is already taken it retries for
~15s and then exits. That is correct when the port is briefly held during a
graceful handover, but it fails the far more common case: a *previous* gateway
that died uncleanly (the ``os._exit`` force-exit path, a faulthandler
dump-then-exit from :mod:`kiro_crew.dashboard.loop_watchdog`, or a ``kill -9``)
left a process — itself or an inheriting child — still holding a LISTEN socket
on the port. Waiting cannot help: nothing will ever release it, so every
relaunch dies with "Port N still in use" until the OS or the user intervenes.

Why not just ``SO_REUSEADDR``
-----------------------------
asyncio's ``create_server`` already sets ``SO_REUSEADDR`` on POSIX, and that
flag only clears ``TIME_WAIT`` remnants — it does **not** let a new process
steal a socket another live process is actively ``LISTEN``ing on. The gateway
integration test (``test_start_site_real_bind_retry``) demonstrates exactly
this: a live occupier blocks the bind until it closes. So the only way to
"rebind cleanly" after an unclean exit is to *reclaim* the port by terminating
the stale holder.

Safety
------
Reclaim is deliberately conservative — it terminates a holder **only** when all
of these hold:

* the holder is identifiable via ``lsof`` (else we cannot know who to signal →
  fall back to the old wait/retry);
* the holder is a *KiroCrew gateway* process — reusing the same structural
  command-line check that gates ``kirocrew stop`` (:func:`_is_kirocrew_process`)
  so we never signal an unrelated process that merely grabbed the port; and
* the holder does **not** answer an HTTP liveness probe — a *responsive*
  gateway is a legitimate owner (e.g. a second gateway on a different
  ``KIROCREW_HOME`` sharing the port) and is never killed.

Complements — does not replace — :class:`kiro_crew.gateway_lock.GatewayLock`,
which enforces the single-*writer* invariant per home (and whose ``flock`` the
kernel auto-releases on death) but says nothing about the *port*.

Every collaborator (listener lookup, identity check, health probe, terminator)
is injectable so the decision logic is unit-testable without real processes,
``lsof``, or process-killing signals — mirroring the testability split used by
:class:`kiro_crew.dashboard.loop_watchdog.LoopStallWatchdog`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from collections.abc import Awaitable, Callable

logger = logging.getLogger("kiro_crew.dashboard.port_reclaim")

# Reclaim outcomes (returned by :func:`reclaim_stale_gateway_port`).
NO_HOLDER = "no_holder"          # port already free (race) — nothing to do
UNAVAILABLE = "unavailable"      # cannot determine/verify holder (lsof/ps missing)
FOREIGN_HOLDER = "foreign_holder"  # held by a non-KiroCrew process — never touched
HEALTHY_PEER = "healthy_peer"    # held by a live, responsive gateway — never touched
RECLAIMED = "reclaimed"          # stale gateway terminated; port should now be free
RECLAIM_FAILED = "reclaim_failed"  # holder identified as stale but could not be killed


def _listeners_on_port(port: int) -> list[int] | None:
    """PIDs holding a ``LISTEN`` socket on *port*, via ``lsof``.

    Returns the (de-duplicated) list of PIDs, an empty list when nothing is
    listening, or ``None`` when ``lsof`` is unavailable so the caller can fall
    back to plain wait/retry instead of guessing.
    """
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        # A wedged lsof is as useless as a missing one — degrade to wait/retry.
        return None
    except subprocess.CalledProcessError:
        # lsof exits non-zero when nothing matches the filter.
        return []
    return list(dict.fromkeys(int(p) for p in out.splitlines() if p.strip().isdigit()))


async def _probe_gateway_healthy(port: int, timeout: float) -> bool:
    """Return ``True`` iff ``127.0.0.1:port`` answers an HTTP request in time.

    A plain TCP connect is insufficient: a wedged gateway's kernel still
    ``accept()``s the connection even though the (blocked) loop never replies,
    so connect-success would misclassify a wedge as healthy. We therefore send a
    minimal request and require an ``HTTP/`` status line back within *timeout*.
    Every step is bounded — including the teardown ``wait_closed`` — so this
    probe can never itself wedge on a half-dead socket (the very failure mode
    the loop watchdog exists to catch).
    """
    try:
        # Probe loopback deliberately: whether the gateway bound 127.0.0.1
        # (local-only) or 0.0.0.0 (all interfaces), it is reachable here, and we
        # only ever want to check the *local* instance — never an external NIC.
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout
        )
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        writer.write(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        await asyncio.wait_for(writer.drain(), timeout)
        data = await asyncio.wait_for(reader.read(16), timeout)
        return data[:5] == b"HTTP/"
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout)
        except (OSError, asyncio.TimeoutError):
            pass


async def _wait_all_exited(
    pids: list[int],
    timeout: float,
    poll: float,
    pid_exited: Callable[[int], bool],
) -> bool:
    """Poll until every pid in *pids* has exited or *timeout* elapses."""
    loops = max(1, int(timeout / poll))
    for _ in range(loops):
        if all(pid_exited(p) for p in pids):
            return True
        await asyncio.sleep(poll)
    return all(pid_exited(p) for p in pids)


async def _terminate_pids(
    pids: list[int],
    *,
    term_wait: float = 3.0,
    kill_wait: float = 2.0,
    poll: float = 0.1,
) -> bool:
    """SIGTERM *pids*, escalating to SIGKILL, and report whether all exited.

    Returns ``False`` immediately if we lack permission to signal a holder (the
    caller then surfaces a clear "run with sudo" style failure rather than
    silently looping).
    """
    from kiro_crew.cli_server import _pid_exited

    def _signal(pid: int, sig: int) -> bool:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass  # already gone — success for our purposes
        except PermissionError:
            return False
        return True

    for pid in pids:
        if not _signal(pid, signal.SIGTERM):
            return False
    if await _wait_all_exited(pids, term_wait, poll, _pid_exited):
        return True

    # Escalate to SIGKILL for any survivor.
    for pid in pids:
        if not _pid_exited(pid) and not _signal(pid, signal.SIGKILL):
            return False
    return await _wait_all_exited(pids, kill_wait, poll, _pid_exited)


async def reclaim_stale_gateway_port(
    port: int,
    *,
    list_listeners: Callable[[int], list[int] | None] = _listeners_on_port,
    is_kirocrew: Callable[[int], bool] | None = None,
    probe_healthy: Callable[[int, float], Awaitable[bool]] = _probe_gateway_healthy,
    terminate: Callable[[list[int]], Awaitable[bool]] | None = None,
    probe_timeout: float = 2.0,
    identity_timeout: float = 5.0,
    log: logging.Logger = logger,
) -> str:
    """Reclaim *port* iff it is held by a stale/unresponsive KiroCrew gateway.

    Returns one of the module-level outcome constants. Only :data:`RECLAIMED`
    (and, transiently, :data:`NO_HOLDER`) means the port is now bindable; the
    other outcomes leave the holder untouched and signal the caller to fall back
    to plain wait/retry.
    """
    listeners = await asyncio.to_thread(list_listeners, port)
    if listeners is None:
        log.warning(
            "Cannot identify what holds port %d (lsof unavailable) — "
            "falling back to wait/retry.",
            port,
        )
        return UNAVAILABLE
    if not listeners:
        # Freed between the failed bind and this probe — the retry will succeed.
        return NO_HOLDER

    # Resolve the identity check (structural command-line match — the same gate
    # as `kirocrew stop`). Bound to an explicit non-Optional local so the call
    # sites below type-check cleanly.
    checker: Callable[[int], bool]
    if is_kirocrew is not None:
        checker = is_kirocrew
    else:
        try:
            from kiro_crew.cli_server import _is_kirocrew_process
        except Exception:  # pragma: no cover - import shape is stable
            log.warning(
                "Cannot load gateway identity check — not reclaiming port %d.", port
            )
            return UNAVAILABLE
        checker = _is_kirocrew_process

    # ``lsof``/``ps`` can stall for seconds (fd enumeration, stale NFS/socket
    # entries). Run the identity filter off the event loop AND cap it with
    # ``identity_timeout``: the shared ``_is_kirocrew_process`` shells out to
    # ``ps`` with no timeout of its own, so a wedged ``ps`` would otherwise hang
    # the worker thread and leave reclaim unresolved. On timeout we degrade to
    # UNAVAILABLE (fall back to wait/retry) rather than block startup.
    try:
        kiro_pids = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: list(dict.fromkeys(pid for pid in listeners if checker(pid)))
            ),
            timeout=identity_timeout,
        )
    except FileNotFoundError:
        log.warning(
            "Cannot verify holder of port %d (ps unavailable) — not reclaiming.", port
        )
        return UNAVAILABLE
    except asyncio.TimeoutError:
        log.warning(
            "Identity check (ps) for port %d did not complete within %.0fs "
            "(possibly a wedged/stale-NFS ps) — not reclaiming.",
            port,
            identity_timeout,
        )
        return UNAVAILABLE

    if not kiro_pids:
        log.error(
            "Port %d is held by a non-KiroCrew process (pids %s) — refusing to "
            "terminate it. Free the port or choose another with --port.",
            port,
            listeners,
        )
        return FOREIGN_HOLDER

    if await probe_healthy(port, probe_timeout):
        log.error(
            "Port %d is owned by a live, responsive KiroCrew gateway (pid %s) — "
            "not reclaiming. Stop it with `kirocrew stop` or use a different port.",
            port,
            kiro_pids,
        )
        return HEALTHY_PEER

    # Re-verify identity immediately before signalling to shrink the TOCTOU /
    # PID-reuse window opened by the health probe: if a stale holder exited and
    # the OS recycled its PID, drop it rather than signal an unrelated process.
    # Same timeout cap as the initial filter so a wedged ps here cannot hang.
    try:
        kiro_pids = await asyncio.wait_for(
            asyncio.to_thread(lambda: [pid for pid in kiro_pids if checker(pid)]),
            timeout=identity_timeout,
        )
    except (FileNotFoundError, asyncio.TimeoutError):
        log.warning(
            "Pre-signal identity re-check for port %d could not complete — "
            "not reclaiming.",
            port,
        )
        return UNAVAILABLE
    if not kiro_pids:
        return NO_HOLDER

    if terminate is None:
        terminate = _terminate_pids
    log.warning(
        "Port %d is held by an unresponsive KiroCrew gateway (pid %s) left by an "
        "unclean exit — terminating it to reclaim the port.",
        port,
        kiro_pids,
    )
    ok = await terminate(kiro_pids)
    if ok:
        log.warning("Reclaimed port %d from stale gateway (pid %s).", port, kiro_pids)
        return RECLAIMED
    log.error(
        "Failed to terminate stale gateway (pid %s) on port %d — you may need to "
        "kill it manually (e.g. `sudo kirocrew stop`).",
        kiro_pids,
        port,
    )
    return RECLAIM_FAILED
