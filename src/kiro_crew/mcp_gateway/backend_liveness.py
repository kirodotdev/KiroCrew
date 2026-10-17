"""Liveness and teardown of a ``Backend``.

The heartbeat ping and the two-condition wedge detector, the terminal backend-gone broadcast
that answers every waiter, the recycle decision for a departing stub's in-flight work, the
idle recycle, and shutdown.

Each function here whose first parameter is ``self`` is a ``Backend`` method: the class
binds it by name, so it reads and writes the backend's own state on the instance, and a
patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_dead_reason``, ``_gone_broadcast``, ``_gone_replay_uris``,
``_init_pending``, ``_init_state``, ``_lease_awaiting_grant``,
``_lease_awaiting_release``, ``_lease_pending_riders``, ``_lease_release_waiters``,
``_lease_replacement_subscribes``, ``_pending_requests``, ``_stderr_task``,
``_stdout_task``, ``_warned_slow_ids``, ``quarantined``.

Instance fields read: ``_inbox_lock``, ``_init_done_event``,
``_last_ping_response_mono``, ``_shutdown_lock``, ``_stub_inboxes``, ``pool_key``,
``process``, ``refcount``, ``reports_inflight``, ``stdin``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any, Mapping, Optional

from kiro_crew import platform_compat
from kiro_crew.constants import SUBAGENT_TIMEOUT_SECS
from kiro_crew.mcp_gateway.backend_leases import _RESOURCES_SUBSCRIBE_METHOD
from kiro_crew.mcp_gateway.backend_mcp_apps import _APPS_STUB_SENTINEL
from kiro_crew.mcp_gateway.backend_wire import BackendGone, _PendingRequest, _write_json_line
from kiro_crew.process_identity import failure_name, with_kill_failure
from kiro_crew.sel import SecurityEventLog

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.backend import Backend

#: The facade's logger: log capture and filters select this code's records by that name.
logger = logging.getLogger("kiro_crew.mcp_gateway.backend")


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
# loop still services ping requests.
#
# It has to sit ABOVE the longest LEGITIMATE in-flight request, or it stops
# being a wedge detector and becomes a deadline: a blocking ``spawn_sub_agents``
# is in flight for as long as its slowest member runs, so a ceiling at or below
# the subagent deadline recycles the backend under a caller whose work is
# healthy, reporting ``backend gone`` while the subagent keeps running detached
# and its result is stranded. The subagent deadline is therefore the binding
# term (``wait``'s 1800s max is well under it), plus a 5-minute margin. An
# operator who raises ``agent.subagent_timeout_secs`` past the default re-opens
# that gap; the load-time clamp bounds how far.
HARD_WEDGE_CEILING_SECS = float(SUBAGENT_TIMEOUT_SECS + 300)

# The ceiling for a backend that REPORTS its in-flight progress
# (``mcp_shared.INFLIGHT_CAPABILITY_KEY``): every call that legitimately runs
# for a long time there is deferred and refreshes its own age every
# ``INFLIGHT_REPORT_SECS``, so an age this old on such a backend is a call that
# stopped progressing -- a worker call hung for a quarter of an hour, which no
# shipped tool does on purpose (the longest synchronous one bounds itself at
# 210s). Recycling then is the self-heal the 3h ceiling above cannot give.
PROGRESS_WEDGE_CEILING_SECS = 900.0


def _is_heartbeat_id(msg_id: Any) -> bool:
    """True if ``msg_id`` is the reserved heartbeat ping id (int or its
    string form, since some backends stringify response ids)."""
    return msg_id == HEARTBEAT_PING_ID or str(msg_id) == str(HEARTBEAT_PING_ID)


#: Requests a departing stub may abandon without the backend being recycled:
#: a ping and the read-only listings run no tool work, so there is nothing a
#: best-effort cancel could fail to stop. ``initialize`` is not listed because
#: it is never pending under a stub's own uuid: the upstream handshake is
#: forwarded under the ``__init__`` sentinel. See ``has_recyclable_in_flight``.
_RECYCLE_EXEMPT_METHODS = frozenset(
    {
        "ping",
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
    }
)


def has_recyclable_in_flight(pending: Mapping[str, Any], stub_uuid: str) -> bool:
    """Whether ``stub_uuid`` owns in-flight work that justifies a recycle.

    ``pending`` is a backend's ``_pending_requests``. Scope B kills a backend
    whose last stub left with work in flight, because the cancel notification
    is best-effort and abandoned tool work must not keep running. A ping or a
    listing request has no such work: killing the backend for one throws away
    a backend that just finished starting, and the client's retry pays a fresh
    spawn behind every other queued backend. Those requests are
    cancelled like any other, but the backend stays pooled for the retry.
    """
    return any(
        getattr(p, "stub_uuid", None) == stub_uuid
        and getattr(p, "method", "") not in _RECYCLE_EXEMPT_METHODS
        for p in pending.values()
    )


async def _broadcast_backend_gone(self: Backend, reason: str) -> None:
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
            pending.stub_uuid,
            inbox,
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
            self._gone_replay_uris.setdefault(pending.replay_stub, set()).add(pending.resource_uri)
    self._pending_requests.clear()
    self._init_pending.clear()
    # Parked lease work is NOT in ``_pending_requests``: a rider waiting
    # on an in-flight grant, a replacement subscribe parked behind a
    # release, and an unsubscribe waiting on a release verdict each hold
    # a client request id that only a backend response would settle —
    # and no response is coming. Answer each with the same synthetic
    # error so no client id hangs forever (a replay parking carries no
    # id and drops silently), then clear the lease coordination state:
    # it gates writes onto a wire that is gone. The routing
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
            parked_uuid,
            inbox,
            (json.dumps(err, separators=(",", ":")) + "\n").encode("utf-8"),
        )


async def _heartbeat_once(self: Backend, now: float) -> str:
    """Classify backend liveness for one heartbeat tick and recover a
    wedged backend in place.

    Returns one of:

    * ``"gone"``   -- the OS has already reaped the subprocess
      (``process.returncode is not None``), or a liveness ping write hit
      a broken pipe. Marked dead; every attached stub receives a
      synthetic error via :meth:`_broadcast_backend_gone`.
    * ``"idle"``   -- no stubs attached (``refcount == 0``). LEFT ALONE:
      the idle-sweep owns eviction of these on its own timer. Recycling
      idle-but-healthy backends here would over-reap them, killing
      backends the idle sweep keeps.
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
    oldest_any_age = 0.0
    oldest_fid: Optional[str] = None
    oldest_pending: Optional[_PendingRequest] = None
    for fid, pending in self._pending_requests.items():
        # A request the backend reported as still progressing is aged from
        # that report: a parked ``wait`` is old, not stuck. "Reported" is
        # the stamp being set, not it being the later of the two: a
        # monotonic clock can read small (Windows counts from boot), so a
        # start stamp can legitimately be below an unset 0.0.
        since_ms = pending.t_progress_ms or pending.t_start_ms
        age = now - (since_ms / 1000.0)
        # The backend answers a cancelled call with no response, so its
        # slot ages forever: it must not reach the hard ceiling on a
        # healthy backend, but it still says a request is outstanding, so
        # with pings also stopped the two-condition rule recycles as it
        # would for any other old request. It stays in the table for
        # attribution and the last-stub recycle (Scope B).
        oldest_any_age = max(oldest_any_age, age)
        if pending.cancelled:
            continue
        if age > oldest_age:
            oldest_age = age
            oldest_fid = fid
            oldest_pending = pending

    if self._pending_requests and oldest_any_age >= HEARTBEAT_TIMEOUT_SECS:
        ping_age = now - self._last_ping_response_mono
        ping_stale = ping_age >= PING_STALE_SECS

        # Hard ceiling: recycle regardless of ping freshness (pathological).
        # A backend that reports progress has refreshed every healthy
        # long call's age above (parked and still-queued calls alike), so
        # it is held to the short ceiling.
        ceiling = PROGRESS_WEDGE_CEILING_SECS if self.reports_inflight else HARD_WEDGE_CEILING_SECS
        if oldest_age >= ceiling:
            self._dead_reason = (
                f"wedged: in-flight request outstanding {oldest_age:.1f}s "
                f">= {ceiling:.0f}s hard ceiling "
                f"(ping_age={ping_age:.1f}s)"
            )
            logger.warning(
                "backend pid=%s pool=%s %s; recycling %s",
                self.pid,
                self.pool_key.human_readable(),
                self._dead_reason,
                self.lifecycle_fields("backend_gone", "deadline"),
            )
            await self._broadcast_backend_gone(self._dead_reason)
            return "wedged"

        # Two-condition: old request + stale ping -> wedged
        if ping_stale:
            self._dead_reason = (
                f"wedged: in-flight request outstanding {oldest_any_age:.1f}s "
                f">= {HEARTBEAT_TIMEOUT_SECS:.0f}s timeout AND "
                f"ping stale {ping_age:.1f}s >= {PING_STALE_SECS:.0f}s"
            )
            logger.warning(
                "backend pid=%s pool=%s %s; recycling %s",
                self.pid,
                self.pool_key.human_readable(),
                self._dead_reason,
                self.lifecycle_fields("backend_gone", "deadline"),
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


async def _cancel_background_tasks(self: Backend) -> None:
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


async def recycle_if_idle(self: Backend) -> bool:
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
            "backend pid=%s quarantined: has %d remaining co-tenants, " "will recycle when drained",
            self.pid,
            self.refcount,
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
        # What the fallback below could not do, named for the record and the
        # audit -- never swallowed. A refused or failed signal here left the
        # process alive while the audit said ``killed``: the same suppressed-
        # failure shape the sub-agent and cron reapers record as ``failed``.
        kill_failed: str | None = None
        try:
            await platform_compat.kill_process_tree_async(pid, platform_compat.SIGKILL)
        except ValueError:
            # Refused pid (non-int, or <= 1 which is a killpg broadcast).
            recycled = False
        except (ProcessLookupError, PermissionError, OSError):
            # Tree already gone or not signalable — fall back to a
            # pid-scoped kill, as this call site did before.
            try:
                await platform_compat.kill_pid_async(pid, platform_compat.SIGKILL)
            except ProcessLookupError:
                # Gone before the signal landed: that IS the kill.
                pass
            except (PermissionError, OSError, ValueError) as exc:
                kill_failed = failure_name(exc)
        if recycled:
            self._dead_reason = "recycled after last stub detached with in-flight work"
            if kill_failed is None:
                logger.info(
                    "backend pid=%s recycled (killed): last stub detached with " "in-flight work",
                    pid,
                )
            else:
                # The pool drops the backend either way (no consumer is left
                # to serve); the reason it carries and the audit row say the
                # process was NOT killed, in the two supervisors' spelling.
                self._dead_reason = with_kill_failure(self._dead_reason, kill_failed)
                logger.warning(
                    "backend pid=%s recycled but its process was not killed: %s",
                    pid,
                    kill_failed,
                )
            # SEL audit: SIGKILLing a pooled backend is a security-relevant
            # action — record it in the HMAC-chained event log regardless
            # of which path (abort frame or plain disconnect) got us here.
            try:
                SecurityEventLog().log_api_access(
                    caller="gatewayd",
                    operation="mcp-gateway.backend-recycle-kill",
                    # Never ``killed`` for a process the signal left alive.
                    outcome="killed" if kill_failed is None else "failed",
                    source="gateway",
                    resources=f"pid={pid} server={self.pool_key.server_name}",
                    error=self._dead_reason,
                )
            except Exception:  # pragma: no cover — audit must never break recycle
                logger.debug("SEL audit for backend recycle kill failed", exc_info=True)
            return True
    return False


async def shutdown(self: Backend, timeout: float = 5.0) -> None:
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
                "escalating to SIGKILL %s",
                self.process.pid,
                timeout,
                self.lifecycle_fields("forced_exit", "deadline"),
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
