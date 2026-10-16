"""Gateway restart and make-live orchestration for Dev Fleet.

Two processes share this module, and the split between them is the security
boundary this module exists to respect:

* The **gateway process** owns the live-target pointer (``service/live_target.py``).
  ``_make_live`` and ``_restart_gateway`` run THERE, behind Dev Fleet's in-gateway
  routes (``gateway_routes.py``), authorised by the dashboard owner's own request.
* The **sandboxed backend** (``server.py``) never touches the pointer file: the OS
  mask over ``live_target.json`` applies to it and to every child it spawns — a
  worktree's ``npm ci`` lifecycle scripts run in the backend's namespace, so any
  file the backend could write, a checkout under build could write too. The
  backend READS pointer state through the gateway instead, via the provider
  installed by :func:`install_pointer_provider`.

Every pointer-derived read the BACKEND makes (``_live_worktree_path``,
``_staged_target_resolved``, ``_staged_cancel_available``) therefore goes through
the provider when one is installed, and answers locally otherwise. ``_staged_target``
itself stays the gateway's synchronous file read: ``_make_live`` — which refuses to
run under a provider at all — uses it under its lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib as _importlib
import os
import re
import secrets
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable, Iterator

from kiro_crew import _facade_mirror
from kiro_crew.apps.builtins.dev_fleet import gateway_service, repository, runtime
from kiro_crew.apps.builtins.dev_fleet.live_leases import (
    _LEASE_CONFIRMS,
    _LEASE_LOSS_EVENTS,
    _POD_STARTS,
    _REMOVAL_LEASE_MAX_OUTSTANDING,
    _REMOVAL_LEASE_RENEW_SECS,
    _REMOVAL_LEASE_TTL_SECS,
    _REMOVAL_LEASES,
    LeaseOutcome,
    _lease_names,
    _RemovalLease,
    _sweep_removal_leases,
    _worktree_key,
    release_removal_lease,
    removal_in_progress,
    renew_removal_lease,
)
from kiro_crew.apps.builtins.dev_fleet.live_plan import (
    _make_live_plan,
    _make_live_status_error,
    _manual_restart_command,
    _staged_notice,
    _undo_binding_holds,
)
from kiro_crew.apps.builtins.dev_fleet.live_service import (
    _LIVE_GATEWAY_UNIT,
    _foreground_backend,
    _foreground_eligible,
    _gateway_backend,
    _gateway_service_active,
    _gateway_start_id,
    _in_pod,
    _launchd_live_worktree,
    _live_user_unit_status,
    _running_checkout,
    _staged_cancel_available_local,
    _staged_target,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.service import live_target


@dataclass(frozen=True)
class PointerState:
    """What the live-target pointer says, resolved against the running gateway.

    ``live`` is the checkout the gateway is RUNNING from (or ``None``), ``staged`` the
    pointer target when a cutover is written but not yet in effect,
    ``staged_cancel_available`` whether the pointer-only cancel of that stage would be
    accepted on this host, and ``previous`` the pointer's validated one-level undo
    target (``live_target.read_previous_target``) or ``None``. One value object rather
    than four calls so a backend fetching it over the broker pays one round trip and
    cannot see a torn view.

    ``live_verified`` is False when ``live`` came from the pointer alone: the gateway
    runs from a packaged / site-packages install, so ``_running_checkout()`` cannot
    confirm the pointer's checkout is the code being executed. The fleet view shows
    that row as "live?" rather than "live".
    """

    live: str | None
    staged: str | None
    staged_cancel_available: bool
    previous: str | None = None
    live_verified: bool = True


class PointerUnavailable(RuntimeError):
    """The pointer state could not be established (the broker did not answer).

    Raised — never swallowed into ``None`` — because ``None`` means "nothing is live /
    nothing is staged", and a prune that read that from a broker outage would delete
    the very worktree a cutover is staged on. Callers decide how to degrade: the
    fleet view reports the outage, destructive paths refuse.
    """


PointerProvider = Callable[[bool], Awaitable[PointerState]]

#: ``None`` in the gateway (answer locally); the backend installs its broker client.
_POINTER_PROVIDER: PointerProvider | None = None


def install_pointer_provider(provider: PointerProvider | None) -> None:
    """Route every pointer-derived read through *provider* (``None`` restores local).

    Called once by ``server.main()`` — the one place that knows it is the sandboxed
    backend — with a client for the gateway's ``GET /api/apps/dev-fleet/live-target``.
    """
    global _POINTER_PROVIDER
    _POINTER_PROVIDER = provider


def running_in_backend() -> bool:
    """True in the sandboxed Dev Fleet backend, False in the gateway.

    ``server.main()`` installs a pointer provider unconditionally, so its presence is
    this process's identity — the same test :func:`_make_live` refuses on. Callers use
    it to keep host-namespace work (a pod token mint) out of the sandbox.
    """
    return _POINTER_PROVIDER is not None


async def pointer_state(*, fresh: bool = False) -> PointerState:
    """The pointer state this process is entitled to: local in the gateway, brokered
    in the backend. ``fresh`` bypasses display caches on both sides.

    The gateway-local ``_staged_target`` reads and validates the pointer file and
    resolves paths — synchronous filesystem work — so it runs on the executor; the
    gateway's other requests must not wait behind a slow data home.
    """
    if _POINTER_PROVIDER is not None:
        return await _POINTER_PROVIDER(fresh)
    loop = asyncio.get_running_loop()
    live_path = await _live_worktree_path(fresh=fresh)
    # Set in the same synchronous step as ``_LIVE_WORKTREE`` and read before the next
    # await, so it describes the path just returned.
    live_verified = _LIVE_VERIFIED

    def _pointer_fields() -> tuple[str | None, str | None]:
        # Staged target and undo history come from the same pointer read: one hop,
        # one document, so the two fields cannot describe different pointers.
        previous = live_target.read_previous_target()
        return _staged_target(), (str(previous) if previous is not None else None)

    staged, previous = await loop.run_in_executor(subprocess_executor(), _pointer_fields)
    # The cancel-availability probe reaches the service manager (a subprocess). The
    # fleet endpoint that consumes this is polled, so it is only paid while a stage
    # exists — with none, the control it gates cannot render and the answer is False.
    cancel = await _staged_cancel_available() if staged is not None else False
    return PointerState(
        live=live_path,
        staged=staged,
        staged_cancel_available=cancel,
        previous=previous,
        live_verified=live_verified,
    )


def acquire_removal_lease(path: str, *, check_cutover: bool = True) -> str | None:
    """Gateway-local: lease *path* for a removal; returns the capability, or ``None``.

    Refused while ``_MAKE_LIVE_LOCK`` is held or a cutover has committed: a removal
    that started under a staging write could delete the target being staged.
    ``check_cutover=False`` is for a caller that HOLDS ``_MAKE_LIVE_LOCK`` itself (an
    in-process removal): the lock is what excludes cutovers there, so re-checking it
    would refuse the very holder.

    Also refused while a pod start on *path* is running in this process
    (:func:`pod_start_reservation`): the pod would run from the checkout the removal
    deletes. The removal refuses itself and is retried later, as for a cutover.
    """
    if check_cutover and (_MAKE_LIVE_LOCK.locked() or _MAKE_LIVE_COMMITTED):
        return None
    if _POD_STARTS.get(_worktree_key(path)):
        return None
    now = _sweep_removal_leases()
    if len(_REMOVAL_LEASES) >= _REMOVAL_LEASE_MAX_OUTSTANDING:
        # Refused, not errored: the holder retries later exactly as for a cutover.
        return None
    token = secrets.token_urlsafe(24)
    _REMOVAL_LEASES[token] = _RemovalLease(
        path=path, token=token, expires_at=now + _REMOVAL_LEASE_TTL_SECS
    )
    return token


#: The backend's end of the lease — three awaitables installed by ``server.main``
#: alongside the pointer provider: ``acquire(path) -> token | None``,
#: ``renew(token) -> bool``, ``release(token) -> None``. ``None`` in the gateway, where
#: the ``live_leases`` registry is called directly.
RemovalLeaseClient = tuple[
    Callable[[str], Awaitable[str | None]],
    Callable[[str], Awaitable[bool]],
    Callable[[str], Awaitable[None]],
]
_REMOVAL_LEASE_CLIENT: RemovalLeaseClient | None = None


def install_removal_lease_client(client: RemovalLeaseClient | None) -> None:
    global _REMOVAL_LEASE_CLIENT
    _REMOVAL_LEASE_CLIENT = client


async def _local_acquire(path: str) -> str | None:
    # Same process as the cutover: the caller holds ``_MAKE_LIVE_LOCK`` (see
    # ``worktree_ops``), which is itself the exclusion — the lease is recorded so
    # ``removal_in_progress`` answers, not to re-arbitrate against that lock.
    return acquire_removal_lease(path, check_cutover=False)


async def _local_renew(token: str) -> bool:
    return renew_removal_lease(token)


async def _local_release(token: str) -> None:
    release_removal_lease(token)


@contextlib.contextmanager
def pod_start_reservation(path: str) -> Iterator[bool]:
    """Hold the checkout at *path* against removal leases while a pod start runs.

    Yields whether the start may go ahead. In the gateway (no lease client installed)
    it yields ``False`` while a removal lease names *path*, live or lapsed inside its
    grace barrier; otherwise it records the start in ``_POD_STARTS`` until the block
    exits, and :func:`acquire_removal_lease` refuses a removal of that worktree in the
    meantime. The check and the record are adjacent with no await between them, on the
    loop that also serves the lease routes.

    In the backend (a lease client is installed) the pod start and every removal already
    take ``worktree_ops._wt_lock``, so this yields ``True`` and records nothing.
    """
    if _REMOVAL_LEASE_CLIENT is not None:
        yield True
        return
    if _lease_names(path):
        yield False
        return
    key = _worktree_key(path)
    _POD_STARTS[key] = _POD_STARTS.get(key, 0) + 1
    try:
        yield True
    finally:
        remaining = _POD_STARTS.get(key, 0) - 1
        if remaining > 0:
            _POD_STARTS[key] = remaining
        else:
            _POD_STARTS.pop(key, None)


@contextlib.asynccontextmanager
async def removal_lease(path: str) -> AsyncIterator[LeaseOutcome]:
    """Hold a renewed removal lease on *path* for the block; yields the outcome.

    In the backend the lease is taken, heartbeated and released through the gateway
    broker; a broker outage at acquisition yields a refusal with cause
    ``"unavailable"`` and a declined acquisition one with cause ``"busy"`` (the removal
    then refuses — it cannot prove no cutover is in flight). In the gateway it is the
    local registry. Callers that cannot wrap a long block in ``try`` test the outcome
    and return their own refusal.

    While the block runs, a heartbeat task renews the lease every
    :data:`_REMOVAL_LEASE_RENEW_SECS`, so it cannot lapse during a long removal
    (pod shutdown, ``_GIT_MUTATION_LOCK`` queueing, the git mutation itself). If a
    renewal is REFUSED — the gateway restarted and forgot the lease, or the token was
    released — the heartbeat records the loss and stops; the block is not cancelled
    from outside, because the git mutation inside it runs uninterruptibly by design and
    an exception delivered mid-``git worktree remove`` would be the corruption this lock
    exists to prevent. Callers that must not START a mutation after a loss check
    :func:`removal_lease_lost` at their mutation boundary.
    """
    acquire, renew, release = (
        _REMOVAL_LEASE_CLIENT
        if _REMOVAL_LEASE_CLIENT is not None
        else (_local_acquire, _local_renew, _local_release)
    )
    try:
        token = await acquire(path)
    except PointerUnavailable:
        yield LeaseOutcome(False, "unavailable")
        return
    if token is None:
        yield LeaseOutcome(False, "busy")
        return
    lost = asyncio.Event()
    last_ok = time.monotonic()

    async def _heartbeat() -> None:
        nonlocal last_ok
        while True:
            await asyncio.sleep(_REMOVAL_LEASE_RENEW_SECS)
            try:
                ok = await renew(token)
            except PointerUnavailable:
                # Transient while the lease still has TTL left. But a gateway that has
                # been unreachable for a whole TTL has let the lease lapse (the grace
                # barrier still holds there) — the holder must treat that as lost too,
                # or an outage would leave it believing in a lease the gateway has dropped.
                if time.monotonic() - last_ok >= _REMOVAL_LEASE_TTL_SECS:
                    lost.set()
                    return
                continue
            if not ok:
                lost.set()
                return
            last_ok = time.monotonic()

    async def _confirm() -> bool:
        """An explicit renewal NOW: the proof a mutation boundary needs."""
        if lost.is_set():
            return False
        try:
            ok = await renew(token)
        except PointerUnavailable:
            ok = False
        if not ok:
            lost.set()
        return ok

    beat = asyncio.create_task(_heartbeat())
    _LEASE_LOSS_EVENTS[path] = lost
    _LEASE_CONFIRMS[path] = _confirm
    try:
        yield LeaseOutcome(True)
    finally:
        _LEASE_LOSS_EVENTS.pop(path, None)
        _LEASE_CONFIRMS.pop(path, None)
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        try:
            await release(token)
        except PointerUnavailable:
            # The lease expires on its own; the gateway is the one that vanished.
            pass


# Which checkout powers the live gateway (the upstream reference showed this
# per-row as is_live; users need to see what occupies the main instance).
_LIVE_WORKTREE: str | None = None
# False only when ``_LIVE_WORKTREE`` is the pointer's target taken on trust because
# the running checkout could not be resolved (see ``_live_worktree_path``).
_LIVE_VERIFIED: bool = True
_LIVE_CHECK_AT: float = 0.0
_LIVE_TTL = 30.0


async def _staged_target_resolved() -> str | None:
    """:func:`_staged_target`, answered by whichever side owns the pointer."""
    if _POINTER_PROVIDER is not None:
        return (await _POINTER_PROVIDER(False)).staged
    return _staged_target()


async def _live_worktree_path(*, fresh: bool = False) -> str | None:
    """Resolve the checkout the live gateway is RUNNING from (or None).

    ``fresh=True`` bypasses the 30s display cache -- destructive callers
    (worktree removal) must never authorize against a stale answer: the
    gateway can switch checkouts within the TTL window.

    Provider-aware: the backend gets this through the gateway broker; the body
    below is the gateway's own resolution.
    """
    if _POINTER_PROVIDER is not None:
        return (await _POINTER_PROVIDER(fresh)).live
    global _LIVE_WORKTREE, _LIVE_CHECK_AT, _LIVE_VERIFIED
    now = time.monotonic()
    if not fresh and _LIVE_CHECK_AT and (now - _LIVE_CHECK_AT) < _LIVE_TTL:
        return _LIVE_WORKTREE
    _LIVE_CHECK_AT = now

    # The live-target pointer outranks every service-definition probe below —
    # but ONLY once the gateway is actually running it. A cutover always writes
    # the pointer, and on a host whose service cannot be driven it writes ONLY
    # the pointer, so the unit's WorkingDirectory still names the checkout the
    # gateway was installed from: reading the definition first would report that
    # stale checkout as live and leave `already_live` and `is_live` wrong.
    #
    # Honouring the pointer unconditionally is the opposite error, and the worse
    # one: between staging and the manual restart the pointer names a checkout
    # the gateway is NOT executing, so the fleet would mark it live while the old
    # image serves real data — the exact wrong conclusion this feature exists to
    # prevent. ``_running_checkout()`` is authoritative for what is executing, so
    # the pointer is only "live" when the two agree; otherwise it is staged
    # (see ``_staged_target``) and resolution falls through to the definition.
    #
    # A packaged install has no running checkout to compare with. The pointer is
    # still the best answer for which row is live (removal guards keep protecting
    # it), but it is unverified: ``_LIVE_VERIFIED`` goes False so the fleet view
    # says "live?" instead of asserting it.
    def _resolve_from_pointer() -> tuple[str | None, bool]:
        # Pointer read, ``_running_checkout`` (resolves this process's path) and the
        # comparison all touch the filesystem: one executor hop for the lot.
        pointed = live_target.read_target()
        if pointed is None:
            return None, True
        running = _running_checkout()
        if running is None:
            return str(pointed), False
        if repository._same_path(str(pointed), str(running)):
            return str(pointed), True
        return str(running), True

    resolved, verified = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), _resolve_from_pointer
    )
    if resolved is not None:
        _LIVE_WORKTREE, _LIVE_VERIFIED = resolved, verified
        return _LIVE_WORKTREE

    def _service_manager() -> str | None:
        # ``shutil.which`` stats every PATH entry, so a stalled PATH mount would
        # stall the gateway's loop: the probe runs off it like every other FS step.
        if sys.platform == "darwin" and shutil.which("launchctl"):
            return "launchd"
        if sys.platform == "linux" and shutil.which("systemctl"):
            return "systemd"
        return None

    manager = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), _service_manager
    )
    if manager == "launchd":
        # launchd has no WorkingDirectory to query: the live target IS whatever
        # the agent's ProgramArguments symlink currently points at. Reading the
        # link is authoritative, needs no service query, and reflects a make-live
        # swap immediately.
        launchd_path = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _launchd_live_worktree
        )
        _LIVE_WORKTREE, _LIVE_VERIFIED = launchd_path, True
        return _LIVE_WORKTREE
    if manager != "systemd":
        _LIVE_WORKTREE, _LIVE_VERIFIED = None, True
        return None
    # Prefer WorkingDirectory: make-live always writes it alongside ExecStart,
    # and the baseline unit sets it too. ``--value`` prints the bare path with
    # no ``WorkingDirectory=`` prefix and, crucially, NO truncation at spaces —
    # so a checkout path containing a space resolves correctly. The old
    # ExecStart ``path=([^ ;]+)`` regex truncates at the first space, which
    # (now that make-live escapes space paths into the drop-in) would leave
    # is_live / already_live perpetually unmatched for such a worktree and
    # drive pointless repeat restarts.
    path = None
    rc, out, _err = await runtime._run_cmd(
        [
            "systemctl",
            "--user",
            "show",
            _LIVE_GATEWAY_UNIT,
            "--property=WorkingDirectory",
            "--value",
        ],
        timeout=5,
    )
    if rc == 0 and out.strip():
        path = out.strip()
    else:
        # Fallback: parse ExecStart's ``path=`` when WorkingDirectory is empty
        # (an older unit that predates the WorkingDirectory= directive).
        rc, out, _err = await runtime._run_cmd(
            ["systemctl", "--user", "show", _LIVE_GATEWAY_UNIT, "-p", "ExecStart"],
            timeout=5,
        )
        if rc == 0 and out:
            m = re.search(r"path=([^ ;]+)", out)
            if m:
                exe = Path(m.group(1))
                # <checkout>/.venv/bin/kirocrew -> <checkout>
                if ".venv" in exe.parts:
                    path = str(exe.parents[2])

    def _normalise() -> str | None:
        try:
            return str(Path(path).resolve()) if path else None
        except OSError:
            return None

    normalised = await asyncio.get_running_loop().run_in_executor(subprocess_executor(), _normalise)
    _LIVE_WORKTREE, _LIVE_VERIFIED = normalised, True
    return _LIVE_WORKTREE


# --- gateway service detection + restart ---

# Single-flights the make-live cutover. Two concurrent cutovers would race on
# the shared drop-in (snapshot -> atomic-write -> daemon-reload -> systemd-run
# -> rollback): one request's failure rollback could restore/delete the OTHER
# request's successful override, restarting the gateway into the wrong
# worktree. The mutation sequence in ``_make_live`` runs under this lock; a
# second concurrent request fails fast with ``busy`` rather than queueing (a
# queued cutover could apply a stale target after the winner already restarted
# the gateway out from under us).
_MAKE_LIVE_LOCK = LoopBoundLock()

# Process-local "cutover committed" latch. ``systemd-run --collect ... restart``
# only SCHEDULES the restart and returns immediately, so ``_MAKE_LIVE_LOCK`` is
# released while the restart is still pending. Without this latch a second
# cutover could then acquire the lock and mutate the drop-in for target B while
# target A's already-scheduled restart tears this backend down mid-write —
# leaving the loaded unit and the persisted drop-in disagreeing. Once a cutover
# is successfully scheduled we set this True (BEFORE returning) and refuse every
# further request for the rest of THIS process's life with ``restart_pending``.
# It is deliberately process-local and never persisted: the fresh gateway the
# restart spawns starts with it clear. Failure paths BEFORE successful
# scheduling never set it, and ``dry_run`` never sets it.
_MAKE_LIVE_COMMITTED = False


async def _gateway_service_reason() -> str | None:
    """Human-readable reason the gateway service cannot be driven, or ``None``.

    Reuses the make-live eligibility codes so one probe explains both controls.
    The live-checkout hint is appended for the case that motivated this field:
    on a packaged desktop app the gateway runs from inside the bundle, so even a
    successful restart would not pick up a Pull+Build of the main checkout — and
    the previous UI said nothing at all.
    """
    if await _gateway_service_active():
        return None
    status = await _live_user_unit_status()
    reason = _make_live_status_error(status)
    if status in {"no_agent", "no_user_unit"}:
        # The pointer read goes through the broker in the backend. An outage there
        # is "unknown", not "no worktree": the hint below is advisory, and raising
        # out of a payload field would collapse the whole fleet view to an error
        # when the rows themselves are fine.
        try:
            unknown_worktree = await _live_worktree_path() is None
        except PointerUnavailable:
            unknown_worktree = False
        if unknown_worktree:
            reason += (
                ". The running gateway does not belong to any known worktree, so "
                "restarting it would not apply a Pull+Build of the main checkout"
            )
    return reason


async def _staged_cancel_available() -> bool:
    """Whether ``_make_live``'s pointer-only cancel of a staged cutover would
    be accepted on this host.

    Mirrors the cancel branch's own precondition (``not can_restart``). The
    pointer-only cancel is deliberately limited to hosts whose service manager
    this app cannot drive: on a drivable host the stage also carries a service
    DEFINITION, so ``_make_live`` refuses the shortcut with
    ``staged_cutover_pending``. The fleet payload reports this so the dashboard
    only offers a cancel control the backend will accept — note it is NOT the
    same signal as ``_gateway_service_active()``, which also goes true for the
    foreground last resort (where ``can_restart`` stays false and the cancel
    DOES work).

    Provider-aware: the backend takes the gateway's answer, because the
    service-manager probe is a fact about the GATEWAY's plane.
    """
    if _POINTER_PROVIDER is not None:
        return (await _POINTER_PROVIDER(False)).staged_cancel_available
    return await _staged_cancel_available_local()


async def _restart_gateway() -> dict:
    """Restart the gateway, preferring the service backend with foreground fallback.

    Tries the platform service manager first (systemd/launchd). When that is
    unavailable or inactive, falls through to the foreground backend — the same
    detach-and-respawn path that Make Live uses on hosts without a drivable
    service manager.

    Returns the pre-restart ``start_id`` so the frontend can poll until a
    DIFFERENT identity appears.
    """
    # Reject while a Make Live cutover is in-flight: restarting mid-staging
    # would tear the gateway down between the pointer write and the reload,
    # leaving persisted and loaded targets diverged. Acquire the lock to
    # prevent a concurrent Make Live from starting while we restart.
    global _MAKE_LIVE_COMMITTED
    if _MAKE_LIVE_COMMITTED:
        return {
            "ok": False,
            "error": "a Make Live cutover is in progress — retry after it completes",
        }
    if _MAKE_LIVE_LOCK.locked():
        return {
            "ok": False,
            "error": "a Make Live cutover is in progress — retry after it completes",
        }
    async with _MAKE_LIVE_LOCK:
        if _MAKE_LIVE_COMMITTED:
            return {
                "ok": False,
                "error": "a Make Live cutover is in progress — retry after it completes",
            }
        # A restart tree-kills the backend, so it must not land while the backend
        # is mid-``git worktree remove``. Checked under the lock: with the lock held
        # no new lease can be granted (``acquire_removal_lease`` refuses), so this
        # answer holds for the rest of the block.
        if removal_in_progress():
            return {
                "ok": False,
                "error": "a worktree removal is in progress — retry once it has completed",
            }

        svc = _gateway_backend()
        service_active = False if svc is None else await svc.active()

        if service_active:
            assert svc is not None  # narrowing: service_active implies svc is not None
            start_id = await _gateway_start_id()
            ok, err = await svc.restart_detached()
            if not ok:
                return {"ok": False, "error": runtime._redact(err)}
            _MAKE_LIVE_COMMITTED = True
            return {"ok": True, "start_id": start_id}

        # Foreground fallback: hosts without a drivable service manager (e.g. AL2
        # with broken sudo, no systemd --user bus) can still restart via the
        # detach-and-respawn path — but only when not confined (status check
        # mirrors _make_live and _gateway_service_active).
        status = await _live_user_unit_status()
        if _foreground_eligible(status):
            fg = _foreground_backend()
            if fg is not None:
                fg_status = await fg.status()
                if fg_status == gateway_service.STATUS_OK:
                    start_id = await _gateway_start_id()
                    ok, err = await fg.restart_detached()
                    if not ok:
                        return {"ok": False, "error": runtime._redact(err)}
                    _MAKE_LIVE_COMMITTED = True
                    return {"ok": True, "start_id": start_id}
                # Foreground eligible but confined/broken — surface the
                # foreground's own refusal reason plus the manual remedy.
                return {
                    "ok": False,
                    "error": (
                        f"foreground gateway cannot restart ({fg_status})"
                        f" — run `{_manual_restart_command()}` to apply the build"
                    ),
                }

        # Neither the service backend nor the foreground fallback can drive the
        # restart — surface the specific reason plus the manual remedy.
        return {
            "ok": False,
            "error": (
                f"{_make_live_status_error(status)}"
                f" — run `{_manual_restart_command()}` to apply the build"
            ),
        }


async def _make_live(
    path: str,
    dry_run: bool = False,
    expected_staged: str | None = None,
    *,
    undo: bool = False,
) -> dict:
    """Repoint the live gateway at *path* — the gateway-process entry point.

    Two duties sit here, around :func:`_make_live_inner` which carries the whole
    validation and cutover sequence (including the ``undo`` binding):

    * **Process boundary.** Only the gateway may WRITE the pointer. In the
      sandboxed backend the pointer is a bind-masked file (``os.replace`` onto it
      is EBUSY) — but the refusal is not about the error it would hit, it is about
      the boundary: a backend that could write the pointer would hand that power to
      every build child in its namespace. The backend installs a pointer provider
      at startup, so its presence is the process's identity.
    * **Cross-process exclusion.** The backend's worktree removal holds a
      :func:`removal_lease` — gateway-held state — across its protection re-check
      and ``git worktree remove``. A live lease refuses the cutover ``busy`` here;
      once the inner sequence holds ``_MAKE_LIVE_LOCK`` no new lease can be granted
      (:func:`acquire_removal_lease` checks that lock), so the window between this
      check and the lock is closed from the other side. A ``dry_run`` mutates
      nothing and skips the check, so a preview never waits on a removal.
    """
    if _POINTER_PROVIDER is not None:
        return {
            "ok": False,
            "code": "wrong_process",
            "error": (
                "make-live must run in the gateway process (POST "
                "/api/apps/dev-fleet/make-live), not in the Dev Fleet backend"
            ),
        }
    if dry_run:
        return await _make_live_inner(
            path, dry_run=True, expected_staged=expected_staged, undo=undo
        )
    if removal_in_progress():
        return {
            "ok": False,
            "code": "busy",
            "error": "a worktree removal is in progress — retry once it has completed",
        }
    return await _make_live_inner(path, dry_run=False, expected_staged=expected_staged, undo=undo)


async def _make_live_inner(
    path: str,
    dry_run: bool = False,
    expected_staged: str | None = None,
    *,
    undo: bool = False,
) -> dict:
    """Repoint the live gateway at *path* by staging the live-target pointer.

    ``undo=True`` binds the request to the pointer's validated
    ``previous_checkout``. The binding is checked before validation and again
    under the single-flight lock, so a banner rendered for an older cutover can
    never repoint a newer live target. A successful undo clears the one-level
    history instead of turning itself into an implicit redo.

    ``expected_staged`` binds a CANCEL to the state the operator confirmed:
    when set, the request proceeds only while the staged target still
    resolves to that path (checked at entry and re-checked under the
    single-flight lock) AND *path* still resolves to the running checkout,
    refusing with ``stage_changed`` otherwise — without both bindings, a
    cancel confirmed against one stage/live pair could silently discard a
    different stage, or fall through to the cutover path and restart the
    gateway into a checkout that is not the live one.

    Validation order (all enforced for ``dry_run`` too): the path is a known,
    existing worktree (``unknown_path`` / ``missing_path``); NOT inside a pod,
    fail-CLOSED on indeterminate pod status (``pod`` / ``pod_indeterminate``);
    not already live (``already_live``); the worktree has its own executable
    ``.venv/bin/kirocrew`` (else Provision -> ``missing_venv`` when absent,
    ``venv_not_executable`` when present but not +x) and a built SPA
    ``dist/index.html`` (else Pull+Build -> ``missing_dist``).

    A real cutover writes the pointer, then bounces the gateway through the
    service manager when there is one we can drive (DETACHED, so it survives our
    own death — mirroring ``_restart_gateway``). When there is not, the pointer
    still stands and the response carries ``staged_only`` plus the one command
    that finishes it: the gateway reads the pointer on ITS next start, whoever
    performs it. Staging deliberately does NOT require a drivable service, which
    is what keeps a ``kirocrew service install`` host (a SYSTEM unit, needing
    root to bounce) able to cut over at all.
    """
    global _MAKE_LIVE_COMMITTED, _LIVE_WORKTREE, _LIVE_CHECK_AT
    # A cutover already scheduled in THIS process. systemd-run has returned but
    # the restart is still pending, so refuse up-front (before any validation or
    # dry_run plan) — any further mutation would race the pending restart.
    if _MAKE_LIVE_COMMITTED:
        return {
            "ok": False,
            "code": "restart_pending",
            "error": (
                "a cutover has been scheduled; the gateway is restarting — "
                "retry after it comes back"
            ),
        }
    target, err = await repository._find_worktree_by_path(path)
    if target is None:
        return {"ok": False, "code": "unknown_path", "error": err}
    real = Path(target["path"])

    # This runs on the GATEWAY's loop, which serves the whole dashboard and every
    # app's proxied traffic: no filesystem read below may happen inline. Every probe
    # of the pointer, the data home or the service drop-in hops to the subprocess
    # executor; only in-memory state and the lock checks stay on the loop.
    _off = asyncio.get_running_loop().run_in_executor

    if not await _off(subprocess_executor(), real.exists):
        return {
            "ok": False,
            "code": "missing_path",
            "error": f"worktree path no longer exists: {real}",
        }

    pod = await _off(subprocess_executor(), _in_pod)
    if pod is None:
        return {
            "ok": False,
            "code": "pod_indeterminate",
            "error": (
                "cannot determine whether this backend runs inside a pod (config "
                "home unresolvable) — refusing make-live to avoid repointing the "
                "live gateway from an unattributable plane"
            ),
        }
    if pod:
        return {
            "ok": False,
            "code": "pod",
            "error": (
                "refusing make-live from inside a pod — a pod is a throwaway test "
                "instance and must never repoint the real live gateway "
                "(run this from the live dashboard)"
            ),
        }

    if undo and not await _off(subprocess_executor(), _undo_binding_holds, real):
        # Pointer read + path comparison: filesystem work, off the loop like every
        # other probe here.
        return {
            "ok": False,
            "code": "undo_changed",
            "error": (
                "Undo is no longer available because the previous live "
                "checkout changed — refresh the fleet before retrying"
            ),
        }

    # A request carrying expected_staged is a CANCEL of that exact stage and
    # nothing else. Validated HERE, before any branching: if the named stage
    # completed or was re-pointed while the confirm dialog sat open, the
    # request must not fall through to the full-cutover path below — that
    # would restart the gateway back into the requested checkout, turning a
    # stale "cancel" into the destructive opposite of what the operator asked.
    # Re-checked under the single-flight lock before the pointer write.
    if expected_staged is not None:
        pending_entry = await _off(subprocess_executor(), _staged_target)
        if pending_entry is None or not await _off(
            subprocess_executor(), repository._same_path, expected_staged, pending_entry
        ):
            now_desc = (
                f"{Path(pending_entry).name} is staged now"
                if pending_entry is not None
                else "nothing is staged now"
            )
            return {
                "ok": False,
                "code": "stage_changed",
                "error": (
                    "the staged cutover changed while you were confirming: "
                    f"{now_desc}, not {Path(expected_staged).name} — refresh "
                    "the fleet and retry"
                ),
            }

    # The live target is a POINTER the gateway resolves at startup, not an edit
    # to this host's service definition — so staging never needs the service
    # manager. Restarting still does, and that is the one thing a `kirocrew
    # service install` SYSTEM unit cannot give us without root: the cutover is
    # staged either way, and when we cannot bounce the gateway ourselves we hand
    # the operator the one command that finishes it. Refusing here instead would
    # make the whole feature unreachable on the most common Linux install.
    svc = _gateway_backend()
    unit_status = await _live_user_unit_status()
    can_restart = svc is not None and unit_status == "ok"
    # LAST RESORT (strictly systemd > launchd > foreground): when no manager is
    # drivable at all, a detached `kirocrew restart` can still finish the
    # cutover (see gateway_service.ForegroundBackend). Probed ONCE per request,
    # mirroring can_restart, so the plan and the act cannot disagree. Only the
    # FOREGROUND_ELIGIBLE codes qualify — a mis-set-up manager keeps its named
    # remedy instead of being bounced behind its back.
    foreground: "gateway_service.ForegroundBackend | None" = None
    if not can_restart and _foreground_eligible(unit_status):
        fg = _foreground_backend()
        if fg is not None and await fg.status() == gateway_service.STATUS_OK:
            foreground = fg

    live = await _live_worktree_path()
    same_as_running = live is not None and await _off(
        subprocess_executor(), repository._same_path, str(real), live
    )
    if undo and (live is None or same_as_running):
        return {
            "ok": False,
            "code": "undo_not_ready",
            "error": (
                "Undo is available only after the cutover has completed and "
                "the new checkout is running"
            ),
        }
    if expected_staged is not None and not same_as_running:
        # A request carrying expected_staged is a CANCEL: it re-pins the
        # checkout the operator saw as live. If the live checkout moved since
        # the dialog (a cutover landed and re-staged in between), the request
        # names a checkout that is not the running one — falling through to the
        # cutover path below would restart the gateway into it, the
        # destructive opposite of a cancel. Refuse instead.
        live_name = Path(live).name if live else "an unknown checkout"
        return {
            "ok": False,
            "code": "stage_changed",
            "error": (
                "the live checkout changed while you were confirming: "
                f"{live_name} is running now, not {real.name} — refresh the "
                "fleet and retry"
            ),
        }
    if same_as_running and await _off(subprocess_executor(), _staged_target) is None:
        # Nothing staged: pointing at the checkout already running is a no-op on
        # EVERY host. This guard sits before the cancel below so that a drivable
        # host cannot turn a harmless repeat click into a real gateway restart by
        # falling through to the cutover path.
        return {
            "ok": False,
            "code": "already_live",
            "error": f"{real.name} is already the live gateway",
        }
    if same_as_running and not can_restart:
        # Pointing at the checkout already running is normally a no-op — EXCEPT
        # while a cutover is staged, where it is the operator cancelling it. The
        # pointer names a different checkout than the running image, so re-pinning
        # the running one is exactly "stay on what is running", and it is the only
        # cancel a non-drivable host can offer: without this the operator's only
        # routes are to complete the cutover into the wrong code and reverse it
        # (two manual restarts) or to hand-delete a keystone-fenced file the
        # product never names.
        #
        # Deliberately limited to hosts this app cannot drive. A drivable host
        # also stages a service DEFINITION naming the staged checkout, and this
        # shortcut only touches the pointer — so the definition would keep naming
        # a checkout nobody intends to run. Once that checkout is pruned the unit
        # fails to start before it ever reads the pointer, turning a recoverable
        # mis-stage into a gateway that will not boot. A drivable host therefore
        # falls through to the full cutover below, which restages the definition
        # and the pointer together and restarts.
        pending_target = await _off(subprocess_executor(), _staged_target)
        if pending_target is None:
            # Defensive re-read: the check above and this one straddle no await,
            # but keeping it means the cancel never builds a plan around a stage
            # that has since disappeared.
            return {
                "ok": False,
                "code": "already_live",
                "error": f"{real.name} is already the live gateway",
            }
        cancel_plan = {
            "action": "cancel_staged_cutover",
            "staged_target": pending_target,
            "keeps_live_target": str(real),
            "pointer_path": str(await _off(subprocess_executor(), live_target.pointer_path)),
            "restart": "not needed",
        }
        # Deleting the pointer IS a mutation, so it owes the same two duties as
        # the cutover below: never act under ``dry_run``, and never touch the
        # pointer outside the single-flight lock.
        if dry_run:
            return {"ok": True, "dry_run": True, "plan": cancel_plan}
        if _MAKE_LIVE_LOCK.locked():
            return {
                "ok": False,
                "code": "busy",
                "error": ("another make-live cutover is in progress"),
            }
        async with _MAKE_LIVE_LOCK:
            if _MAKE_LIVE_COMMITTED:
                return {
                    "ok": False,
                    "code": "restart_pending",
                    "error": (
                        "a cutover has been scheduled; the gateway is restarting — "
                        "retry after it comes back"
                    ),
                }
            if removal_in_progress():
                # Granted between the wrapper's check and this lock; none can be
                # granted from here on, so the refusal is complete.
                return {
                    "ok": False,
                    "code": "busy",
                    "error": "a worktree removal is in progress — retry once it has completed",
                }
            # Re-read under the lock: the awaits above mean the stage may have
            # been completed or re-pointed since the entry check, and cancelling
            # a stage that is already gone would delete a pointer someone else
            # just wrote.
            pending_now = await _off(subprocess_executor(), _staged_target)
            if pending_now is None:
                return {
                    "ok": False,
                    "code": "already_live",
                    "error": f"{real.name} is already the live gateway",
                }
            if expected_staged is not None and not await _off(
                subprocess_executor(),
                repository._same_path,
                expected_staged,
                pending_now,
            ):
                # The stage moved between the entry check and the lock: this
                # cancel was confirmed against a different target, so acting
                # would discard a stage the operator never saw.
                return {
                    "ok": False,
                    "code": "stage_changed",
                    "error": (
                        "the staged cutover changed while you were confirming: "
                        f"{Path(pending_now).name} is staged now, not "
                        f"{Path(expected_staged).name} — refresh the fleet and retry"
                    ),
                }
            # Re-pin the RUNNING checkout rather than deleting the pointer.
            # Deleting only means "stay here" when the running image is the
            # installed build; if this checkout was itself selected by an earlier
            # cutover, the pointer is the only record of that choice, so removing
            # it would silently demote the operator back to the installed build
            # on the next restart — the opposite of the cancel they asked for.
            # Writing is idempotent when the pointer already named it.
            loop = asyncio.get_running_loop()
            try:
                prior_pointer = await loop.run_in_executor(
                    subprocess_executor(), live_target.snapshot
                )
            except (OSError, ValueError) as exc:
                return {
                    "ok": False,
                    "code": "write_failed",
                    "error": (
                        "refusing to cancel the staged cutover: the staged pointer "
                        "exists but could not be read, so a failed cancel could not "
                        f"be rolled back: {runtime._redact(str(exc))}"
                    ),
                }
            try:
                await loop.run_in_executor(subprocess_executor(), live_target.write_target, real)
            except (live_target.InvalidTarget, OSError) as exc:
                # InvalidTarget refuses before anything is written. OSError can
                # arrive AFTER the pointer has been replaced, because
                # write_target re-applies the owner-only mode as its last step —
                # so a failure there would otherwise leave a code-execution input
                # in place with inherited permissions while this call reported
                # failure. Roll the pointer back so the cancel is all-or-nothing,
                # and only when there was one: restore(None) UNPINS (publishes the
                # absent-equivalent stub), which is the demotion this branch
                # exists to avoid.
                rolled_back = True
                if prior_pointer is not None:
                    rolled_back = await loop.run_in_executor(
                        subprocess_executor(), live_target.restore, prior_pointer
                    )
                detail = (
                    ""
                    if rolled_back
                    else (
                        " The rollback also failed, so the pointer may name the "
                        "running checkout without owner-only permissions — check it "
                        "before the next restart."
                    )
                )
                return {
                    "ok": False,
                    "code": "write_failed",
                    "error": (
                        "refusing to cancel the staged cutover: the running "
                        "checkout could not be re-pinned as the live target: "
                        f"{runtime._redact(str(exc))}.{detail}"
                    ),
                }
            _LIVE_WORKTREE = None
            _LIVE_CHECK_AT = 0.0
            return {
                "ok": True,
                "cancelled": True,
                "target": str(real),
                "plan": cancel_plan,
                "notice": (
                    f"Staged cutover cancelled. {real.name} stays the live "
                    f"target and no restart is needed."
                ),
            }
    if same_as_running:
        # Drivable host with a stage pending. The pointer-only cancel above is
        # unsafe here (it would leave the service definition naming the staged
        # checkout), but falling through to the full cutover would bounce a live
        # gateway carrying real sessions in response to a request that reads as
        # "keep running what is already running". Refuse and name both real
        # exits instead: surprising an operator in the destructive direction is
        # worse than doing nothing.
        pending = await _off(subprocess_executor(), _staged_target)
        pending_name = Path(pending).name if pending else "another checkout"
        return {
            "ok": False,
            "code": "staged_cutover_pending",
            "error": (
                f"a cutover to {pending_name} is already staged. Dev Fleet can "
                f"restart this host, so cancelling by re-pointing here would leave "
                f"the service definition naming {pending_name}. Make {pending_name} "
                f"live to complete the cutover, or restart the gateway to apply it."
            ),
        }

    kcbin = real / ".venv" / "bin" / "kirocrew"
    dist_index = real / "src" / "kiro_crew" / "static" / "dist" / "index.html"

    def _validate_artifacts_sync() -> tuple[str, str] | None:
        """Check the CLI binary and built dist on the executor thread.

        Returns ``(code, error)`` when a required artifact is absent or
        non-executable, ``None`` when both are present and the binary is
        executable.  Running off the event loop prevents a slow or
        network-backed filesystem from stalling all gateway requests.
        """
        if not kcbin.is_file():
            return (
                "missing_venv",
                (
                    f"{real.name} has no .venv/bin/kirocrew — Provision it first "
                    "(row menu \u2192 Provision) before making it live"
                ),
            )
        # A present-but-non-executable binary is worse than a missing one: the
        # drop-in gets written and the old gateway is stopped, but the
        # replacement can never start (systemd ExecStart requires +x) — leaving
        # NO gateway running.  Gate on the exec bit with a DISTINCT, actionable
        # code.
        if not os.access(kcbin, os.X_OK):
            return (
                "venv_not_executable",
                (
                    f"{real.name} has a non-executable .venv/bin/kirocrew — run "
                    "`chmod +x` on it or re-Provision the worktree before making "
                    "it live (a non-executable binary stops the live gateway but "
                    "cannot start the replacement, leaving no gateway running)"
                ),
            )
        if not dist_index.is_file():
            return (
                "missing_dist",
                (
                    f"{real.name} has no built dashboard "
                    "(src/kiro_crew/static/dist/index.html) — run Pull+Build "
                    "first; cutover without a built dist serves a broken dashboard"
                ),
            )
        return None

    # Early probe: surface an obvious missing-artifact error before reaching
    # the plan or the lock.  Not authoritative — a concurrent provision or
    # rebuild can change these artifacts between this check and the cutover
    # lock below.  The authoritative re-validation happens inside the lock.
    _loop = asyncio.get_running_loop()
    artifact_err = await _loop.run_in_executor(subprocess_executor(), _validate_artifacts_sync)
    if artifact_err is not None:
        code, msg = artifact_err
        return {"ok": False, "code": code, "error": msg}

    def _plan_sync() -> dict:
        # validate() stats the target; keep it off the loop with the other probes.
        return _make_live_plan(real, kcbin, svc=svc if can_restart else None, foreground=foreground)

    try:
        plan = await _off(subprocess_executor(), _plan_sync)
    except live_target.InvalidTarget as exc:
        return {
            "ok": False,
            "code": "unsafe_path",
            "error": (
                "refusing make-live: the worktree path cannot be used as a live "
                f"target: {runtime._redact(str(exc))}"
            ),
        }
    except gateway_service._UnsafeTargetValue as exc:
        return {
            "ok": False,
            "code": "unsafe_path",
            "error": (
                "refusing make-live: the worktree path is not safely representable "
                "in a service definition (contains control characters): "
                f"{runtime._redact(str(exc))}"
            ),
        }
    plan["target"] = str(real)
    if dry_run:
        return {"ok": True, "dry_run": True, "plan": plan}

    # Serialize the mutation sequence: two concurrent cutovers racing on the
    # shared drop-in (snapshot -> write -> reload -> restart -> rollback) could
    # have one request's rollback restore/delete the OTHER's successful
    # override, restarting into the wrong worktree. Fail fast with ``busy`` on
    # contention rather than queueing — a queued cutover would apply a stale
    # target after the winner already restarted the gateway. The check and the
    # acquire are atomic here (no ``await`` between them on the single-threaded
    # event loop), so the busy response cannot itself race the lock.
    if _MAKE_LIVE_LOCK.locked():
        return {"ok": False, "code": "busy", "error": ("another make-live cutover is in progress")}
    async with _MAKE_LIVE_LOCK:
        # Re-check the committed latch now that we hold the lock. A request
        # that passed the entry check just before the WINNING cutover latched
        # (the entry check and the lock acquire are separated by awaits) would
        # otherwise fall through here and mutate the drop-in a second time while
        # the winner's restart is already tearing us down.
        if _MAKE_LIVE_COMMITTED:
            return {
                "ok": False,
                "code": "restart_pending",
                "error": (
                    "a cutover has been scheduled; the gateway is restarting — "
                    "retry after it comes back"
                ),
            }
        if removal_in_progress():
            # Granted between the wrapper's check and this lock; none can be granted
            # from here on, so the refusal is complete.
            return {
                "ok": False,
                "code": "busy",
                "error": "a worktree removal is in progress — retry once it has completed",
            }
        # Re-validate artifacts inside the lock: a concurrent provision or
        # rebuild may have changed the binary or dist between the early probe
        # above and now.  The cutover commits the exact state on disk at this
        # moment, so these are the artifacts it actually stages.
        artifact_err = await _loop.run_in_executor(subprocess_executor(), _validate_artifacts_sync)
        if artifact_err is not None:
            code, msg = artifact_err
            return {"ok": False, "code": code, "error": msg}
        if undo and not await _off(subprocess_executor(), _undo_binding_holds, real):
            return {
                "ok": False,
                "code": "undo_changed",
                "error": (
                    "Undo is no longer available because the previous live "
                    "checkout changed — refresh the fleet before retrying"
                ),
            }
        # Snapshot the prior live target BEFORE staging so a failed cutover can
        # be rolled back — a persisted pointer would otherwise silently activate
        # on the NEXT unrelated restart. Staging itself is atomic (temp file +
        # os.replace), so a partial write can never leave a truncated pointer
        # either.
        #
        # An UNREADABLE (as opposed to absent) prior pointer aborts here, before
        # anything is staged: restore interprets None as "there was nothing
        # here" and DELETES the pointer, so continuing would let a failed
        # restart destroy a live target we merely could not read.
        try:
            prior_content = await _off(subprocess_executor(), live_target.snapshot)
        except (OSError, ValueError) as exc:
            # ValueError covers an undecodable pointer: it exists, so rollback
            # cannot treat it as absent (that DELETES it), and the cutover is
            # refused rather than made unreversible.
            return {
                "ok": False,
                "code": "write_failed",
                "error": (
                    "refusing make-live: the current live target exists but "
                    f"could not be read, so a failed cutover could not be rolled "
                    f"back: {runtime._redact(str(exc))}"
                ),
            }
        # A drivable service may ALSO carry staging from an earlier cutover whose
        # definition names a worktree directly. Leaving that definition pinned to
        # a stale checkout is a live landmine: once that worktree is pruned the
        # unit's ExecStart binary is gone, the service fails EXEC on its next
        # start, and the pointer is never even read — no gateway comes up. So the
        # definition is restaged alongside the pointer whenever we can drive it,
        # keeping the two in agreement and the ExecStart binary always present.
        prior_definition: str | None = None
        if can_restart:
            assert svc is not None
            try:
                prior_definition = await _off(subprocess_executor(), svc.snapshot)
            except OSError as exc:
                return {
                    "ok": False,
                    "code": "write_failed",
                    "error": (
                        "refusing make-live: the current service definition exists "
                        "but could not be read, so a failed cutover could not be "
                        f"rolled back: {runtime._redact(str(exc))}"
                    ),
                }

        def _unwind_sync() -> bool:
            """Restore both staged surfaces. False when either did not land."""
            ok = live_target.restore(prior_content)
            if can_restart and svc is not None:
                ok = svc.rollback(prior_definition) and ok
            return ok

        async def _unwind() -> bool:
            # Both halves block: restore() ends in restrict_to_owner, which
            # rewrites a DACL on Windows, and svc.rollback() rewrites the service
            # definition. Offload them for the same reason the write below is
            # offloaded — an unwind must not stall every other gateway request for
            # the duration of blocking filesystem work.
            return await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), _unwind_sync
            )

        try:
            # write_target ends in restrict_to_owner, which rewrites a DACL
            # on Windows. Run it off the loop so a cutover cannot stall every
            # other gateway request for the duration of that subprocess.
            loop = asyncio.get_running_loop()

            def _write_pointer_sync() -> Path:
                previous = None
                if not undo and live is not None:
                    try:
                        previous = live_target.validate(live)
                    except live_target.InvalidTarget:
                        # Make Live remains available when the running checkout
                        # is not itself provisioned enough to be a safe Undo
                        # destination. The pointer simply carries no history.
                        pass
                return live_target.write_target(
                    real,
                    previous_checkout=previous,
                )

            await loop.run_in_executor(subprocess_executor(), _write_pointer_sync)
        except live_target.InvalidTarget as exc:
            return {"ok": False, "code": "unsafe_path", "error": runtime._redact(str(exc))}
        except OSError as exc:
            return {
                "ok": False,
                "code": "write_failed",
                "rolled_back": await _unwind(),
                "error": runtime._redact(str(exc)),
            }

        # Nothing MANAGED bounces the gateway on this host. When the foreground
        # last resort is usable it finishes the cutover below; otherwise the
        # cutover is STAGED and the operator finishes it — reported as a success
        # with the exact command, not a failure: the pointer is written and
        # correct, and the next start of the gateway — however it happens —
        # comes up on the new target. The staged-only outcome is deliberately
        # NOT latched as committed: no restart is pending, so a subsequent
        # cutover to a different worktree must stay allowed.
        if not can_restart:
            _LIVE_WORKTREE = None
            _LIVE_CHECK_AT = 0.0
            if foreground is not None:
                # Capture identity BEFORE establishing the restart, mirroring
                # the drivable path: the detached bounce can tear this process
                # down at any moment after the spawn.
                start_id = await foreground.start_id()
                restarted, fg_err = await foreground.restart_detached()
                if restarted:
                    # A restart IS pending now — latch exactly as the drivable
                    # path does, so no further cutover mutates the pointer
                    # while the detached `kirocrew restart` is acting on it.
                    _MAKE_LIVE_COMMITTED = True
                    return {
                        "ok": True,
                        "cutover": True,
                        "target": str(real),
                        "plan": plan,
                        "start_id": start_id,
                    }
                # FAIL SAFE: the spawn was never established, so nothing has
                # been signalled and the gateway is untouched. The pointer
                # stays staged (it is written and correct) and the operator
                # gets the exact status-quo advisory. Rolling the pointer back
                # here would be strictly worse: it would turn "finish with one
                # command" into "start over".
                runtime.logger.warning(
                    "foreground restart could not be established (%s); "
                    "falling back to the manual-restart advisory",
                    fg_err,
                )
                plan = {**plan, "restart": "manual", "manual_restart": _manual_restart_command()}
            return {
                "ok": True,
                "cutover": True,
                "staged_only": True,
                "target": str(real),
                "plan": plan,
                "manual_restart": _manual_restart_command(),
                "notice": _staged_notice(real.name, unit_status),
            }
        assert svc is not None  # can_restart implies a backend

        staged, code, err = await svc.stage(real, kcbin)
        if not staged:
            rolled_back = await _unwind()
            # Re-read definitions so the loaded config matches the restored disk
            # state rather than the rejected override.
            await svc.reload()
            return {
                "ok": False,
                "code": code,
                "rolled_back": rolled_back,
                "error": runtime._redact(err),
            }

        # The restart tears down THIS backend with the gateway, so it is handed
        # to the service manager to perform (systemd-run on Linux, launchd's
        # stop/relaunch transaction on macOS) so it survives our own death. Capture the
        # pre-restart identity FIRST so the dashboard reuses the same handshake
        # it uses for restart-gateway (wait for a DIFFERENT start id, not "a 200
        # came back") -- a cutover is just a restart into different code, so it
        # has the identical early-200 hazard. None-safe (see _gateway_start_id).
        start_id = await _gateway_start_id()
        restarted, err = await svc.restart_detached()
        if not restarted:
            rolled_back = await _unwind()
            await svc.reload()
            return {
                "ok": False,
                "code": "restart_failed",
                "rolled_back": rolled_back,
                "error": runtime._redact(err),
            }

        # COMMITTED: the restart is scheduled (the call returns before it
        # lands). Latch process-locally BEFORE returning so no further cutover
        # can mutate the live target while the restart is pending — the fresh
        # process the restart spawns starts with this clear.
        _MAKE_LIVE_COMMITTED = True

        # Invalidate the live-worktree cache so the next fleet poll re-resolves
        # the live checkout.
        _LIVE_WORKTREE = None
        _LIVE_CHECK_AT = 0.0

        return {
            "ok": True,
            "cutover": True,
            "target": str(real),
            "plan": plan,
            "start_id": start_id,
        }


__all__ = (
    "PointerProvider",
    "LeaseOutcome",
    "PointerState",
    "PointerUnavailable",
    "RemovalLeaseClient",
    "_GATEWAY_SERVICE_ACTIVE",
    "_GATEWAY_SERVICE_CHECK_AT",
    "_GATEWAY_SERVICE_TTL",
    "_LIVE_CHECK_AT",
    "_LIVE_GATEWAY_LABEL",
    "_LIVE_GATEWAY_UNIT",
    "_LIVE_TTL",
    "_LIVE_WORKTREE",
    "_MAKE_LIVE_COMMITTED",
    "_MAKE_LIVE_LOCK",
    "_SD_CTRL_RE",
    "_SD_NEEDS_QUOTE_RE",
    "_UnsafeUnitValue",
    "_dropin_content",
    "_dropin_path",
    "_foreground_backend",
    "_foreground_eligible",
    "_gateway_backend",
    "_gateway_label",
    "_gateway_service_active",
    "_gateway_service_reason",
    "_gateway_start_id",
    "_gateway_unit_name",
    "_in_pod",
    "_launchd_live_worktree",
    "_live_user_unit_status",
    "_live_worktree_path",
    "_make_live",
    "_make_live_inner",
    "_make_live_plan",
    "_make_live_status_error",
    "_manual_restart_command",
    "_own_checkout_path",
    "_restart_gateway",
    "_running_checkout",
    "_sd_value",
    "_staged_cancel_available",
    "_staged_cancel_available_local",
    "_staged_notice",
    "_staged_target",
    "_staged_target_resolved",
    "acquire_removal_lease",
    "confirm_removal_lease",
    "install_removal_lease_client",
    "pod_start_reservation",
    "release_removal_lease",
    "removal_lease_lost",
    "renew_removal_lease",
    "removal_in_progress",
    "removal_lease",
    "install_pointer_provider",
    "pointer_state",
)


# This module is the facade of Dev Fleet's live-target and Make Live code, and every name is
# reached through it as ``live.<name>`` (``server.py`` re-exports ``__all__``): a read is
# served by the owner that binds the name, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners
# never import this module. The state the cutover shares stays here: the pointer provider,
# the live-worktree cache, the cutover lock and latch, lease acquisition and the backend's
# lease client, the restart and the cutover itself. New work goes to the owner of its
# responsibility:
#   ``live_leases`` -- the removal-lease and pod-start registries
#   ``live_service`` -- the checkout probes discovery uses (own package, launchd program,
#     running checkout, staged target, the local staged-cancel check) and the live gateway
#     as a service: unit, backend, probes, drop-in
#   ``live_plan`` -- a cutover's dry-run plan, operator-facing text, and the Switch-back
#     undo-binding check (``_undo_binding_holds``)

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.builtins.dev_fleet.live_leases",
    "kiro_crew.apps.builtins.dev_fleet.live_service",
    "kiro_crew.apps.builtins.dev_fleet.live_plan",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(sys.modules[__name__], _OWNERS, excluded=()).served)
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.apps.builtins.dev_fleet.live_leases import (  # noqa: F401
        _REMOVAL_LEASE_GRACE_SECS,
        _renewable_removal_lease,
        confirm_removal_lease,
        removal_lease_lost,
    )
    from kiro_crew.apps.builtins.dev_fleet.live_service import (  # noqa: F401
        _GATEWAY_SERVICE_ACTIVE,
        _GATEWAY_SERVICE_CHECK_AT,
        _GATEWAY_SERVICE_TTL,
        _LIVE_GATEWAY_LABEL,
        _SD_CTRL_RE,
        _SD_NEEDS_QUOTE_RE,
        _dropin_content,
        _dropin_path,
        _gateway_label,
        _gateway_unit_name,
        _own_checkout_path,
        _sd_value,
        _UnsafeUnitValue,
    )

if TYPE_CHECKING:  # names it serves from outside the owner family, from their homes
    from kiro_crew import (  # noqa: F401
        platform_compat,
    )
    from kiro_crew.instances import (  # noqa: F401
        run_marker,
    )
