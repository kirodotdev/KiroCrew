"""Local machine: the local controller, which runs one connector per active grant.

It lives in the local gateway process, or in the desktop app's own host
(:mod:`.desktop_host`) when the app's window points at a remote gateway and no
local gateway runs. Only the process holding the machine's
:class:`~.controller_lock.ControllerLock` runs connectors. It starts a connector for each active
grant while the local switch is on, and stops a connector the moment its grant
is revoked, the local Off is pressed or the switch turns off. Stopping a
connector stops every process tree it started; nothing is deleted.

Sharing renews automatically for as long as it is on. Before each dial the
credential is renewed over SSH when it has less than :data:`RENEW_BEFORE_SECS`
left by this computer's clock, already expired included, and at once after the
gateway's authenticated ``expired``. There is no allowance to spend: the remote
renews a credential iff its record is still active at that credential's
generation, so renewing cannot undo a Disconnect or Remove.

What ends a grant (a relay on the forwarded port can forge any close code,
HTTP status or unauthenticated frame, so none of those may):

* an authenticated ``refusal`` naming an explicit end
  (:data:`.protocol.TERMINAL_REFUSALS`: ``revoked``, ``removed``, a newer
  enrollment, another computer or owner);
* the same code in the remote's own answer over SSH to a renewal, or an answer
  naming another gateway or computer;
* a local terminal state: confinement unavailable (preparing or spawning the
  self-test included), sealed values that fail to open.

Everything else (a run that never got the authenticated ``admitted``, a MAC or
protocol failure, lost liveness, a close, an upgrade answer, any other refusal
code, SSH not reaching the remote, a local grant or switch read that failed) is
a failed attempt: back-off doubles from
:data:`BACKOFF_MIN_SECS` to :data:`BACKOFF_MAX_SECS` and resets only after an
authenticated admission, with the reason shown.

A grant that ended for good is recorded on the grant itself
(:meth:`.local_store.LocalGrantStore.set_ended`) as well as in
:attr:`LocalController._ended`: reconcile never starts it again, in this or a
later process, until a new consent replaces it or it is revoked. A failed write
of that record is retried on every reconcile while the grant stays stopped. A
self-test that passed is cached per grant for this controller's life, so a
redial never runs it again.

Order of a local Off (B01, R8, R9): pending consents are dropped; the grants are
revoked in the local vault, with the Off generation bumped and one remote revoke
queued per (instance, device) pair in that same write, so every connector refuses
new work at once even offline or with the keyring locked; then the connectors and
their processes stop, each within :data:`STOP_BOUND_SECS`; only then is each
remote gateway told (:meth:`LocalController.drain_pending`). A remote that cannot
be reached keeps its entry and is retried with back-off by the reconcile loop; the
grant stays revoked meanwhile, and nothing re-enrolls it.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
from typing import Any, Awaitable, Callable, Final, Optional

from kiro_crew.connected_computers.confinement import (
    ConfinementUnavailable,
    plan_for_this_machine,
    self_test,
)
from kiro_crew.connected_computers.connector import (
    AuthenticatedRefusal,
    Connector,
    ConnectorRefused,
)
from kiro_crew.connected_computers.controller_lock import ControllerLock
from kiro_crew.connected_computers.enroll import EnrollmentFailed
from kiro_crew.connected_computers.flag import local_enabled
from kiro_crew.connected_computers.identity import (
    hostname_slug,
    install_fingerprint,
    local_computer_name,
)
from kiro_crew.connected_computers.local_store import (
    Grant,
    LocalGrantStore,
    ProtectionUnavailable,
    SealBroken,
)
from kiro_crew.connected_computers.protocol import REMOTE_OFF_STATUS

logger = logging.getLogger(__name__)

#: Renew when the credential has less than this left (or none) by this clock.
RENEW_BEFORE_SECS: Final[float] = 3600.0
#: Reconnect back-off bounds.
BACKOFF_MIN_SECS: Final[float] = 5.0
BACKOFF_MAX_SECS: Final[float] = 300.0
#: How often grants are re-read to pick up a revocation made by another process.
RECONCILE_SECS: Final[float] = 3.0
#: Longest wait for one connector to stop its processes (their own grace is 3 s)
#: before its task is cancelled; a remote revoke is attempted only after it.
STOP_BOUND_SECS: Final[float] = 10.0
#: A grant's state while another process on this computer runs its connector.
ELSEWHERE_STATE: Final[str] = "run by another Kiro Crew process on this computer"


def connect_url(local_port: int) -> str:
    """The connector socket through the Remote Crew forward on this machine."""
    return f"ws://127.0.0.1:{int(local_port)}/api/connected-computers/connect"


class LocalController:
    def __init__(
        self,
        store: LocalGrantStore,
        *,
        instance_lookup: Callable[[str], Any],
        plan_factory: Callable[[], Any] = plan_for_this_machine,
        clock: Callable[[], float] = time.time,
        lock: Optional[ControllerLock] = None,
        enabled: Optional[Callable[[], bool]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.store = store
        #: The wait between a grant's attempts (its back-off).
        self._sleep = sleep
        #: The local switch; the desktop host passes its own reader.
        self._enabled = enabled if enabled is not None else local_enabled
        self._instance = instance_lookup
        self._plan_factory = plan_factory
        self._clock = clock
        #: None runs connectors unconditionally (a test's own controller); the
        #: gateway and the desktop host each pass the machine's lock.
        self._lock = lock
        self._elsewhere = False
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._connectors: dict[str, Connector] = {}
        self._status: dict[str, str] = {}
        #: Grants whose connector ended for good, with the state shown for them;
        #: reconcile never restarts one while its grant stays active. Also
        #: persisted on the grant (``Grant.ended``) for the next process.
        self._ended: dict[str, str] = {}
        #: Ended grants whose ``set_ended`` write failed: retried by reconcile.
        self._unsaved: dict[str, str] = {}
        #: Grants whose confinement self-test passed, with their plan: a redial
        #: or a restarted grant task never runs it again in this controller.
        self._tested: dict[str, Any] = {}
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._drain_task: Optional[asyncio.Task[None]] = None
        #: Held around a consent's enrollment and around each remote revoke, so
        #: a queued revoke never lands on the pair a consent is re-enrolling.
        self.pair_lock = asyncio.Lock()

    # -- status -------------------------------------------------------------------
    def status(self) -> list[dict[str, Any]]:
        out = []
        for grant in self.store.list():
            row = grant.public()
            if not grant.active:
                row["state"] = "revoked"
            elif self._elsewhere:
                row["state"] = ELSEWHERE_STATE
            elif grant.grant_id in self._unsaved:
                row["state"] = f"{self._unsaved[grant.grant_id]} (not yet saved)"
            else:
                row["state"] = (
                    self._ended.get(grant.grant_id)
                    or grant.ended
                    or self._status.get(grant.grant_id, "starting")
                )
            out.append(row)
        return out

    @property
    def elsewhere(self) -> bool:
        """Another process on this computer holds the controller lock."""
        return self._elsewhere

    # -- lifecycle ----------------------------------------------------------------
    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.ensure_future(self._run_forever())

    async def _run_forever(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:  # noqa: BLE001 -- the next tick retries
                logger.warning("connected computers: reconcile failed", exc_info=True)
            await asyncio.sleep(RECONCILE_SECS)

    async def reconcile(self) -> None:
        from kiro_crew.connected_computers.disable import startup_settled

        # Consent redemption also reconciles directly, outside the config watcher.
        if not await startup_settled():
            await self.stop_all()
            return
        # Off the loop: without a live config snapshot the switch is read from
        # disk, and a stalled read must not hold the watchdogs or a stop.
        if not await asyncio.to_thread(self._enabled):
            await self.stop_all()
            return
        if self._lock is not None and not await asyncio.to_thread(self._lock.acquire):
            # The other host of this computer's controller runs its connectors.
            self._elsewhere = True
            await self.stop_all()
            return
        self._elsewhere = False
        active = {g.grant_id: g for g in await asyncio.to_thread(self.store.active)}
        for grant_id in list(self._tasks):
            if grant_id not in active:
                await self.stop(grant_id)
        for cache in (self._ended, self._tested, self._unsaved):
            for grant_id in list(cache):
                if grant_id not in active:
                    # Revoked or replaced: a new consent is a new grant id.
                    del cache[grant_id]
        await self._save_ended()
        for grant_id, grant in active.items():
            if grant.ended:
                # Ended for good in this or an earlier process.
                self._ended.setdefault(grant_id, grant.ended)
            if grant_id in self._ended:
                continue
            task = self._tasks.get(grant_id)
            if task is None or task.done():
                self._tasks[grant_id] = asyncio.ensure_future(self._run_grant(grant))
        now = self._clock()
        pending = await asyncio.to_thread(self.store.pending_forgets)
        if any(p.next_try <= now for p in pending):
            self._kick_drain()

    async def stop(self, grant_id: str) -> None:
        """Stop one grant's connector and processes, within :data:`STOP_BOUND_SECS`."""
        task = self._tasks.pop(grant_id, None)
        connector = self._connectors.pop(grant_id, None)
        if connector is not None:
            try:
                await asyncio.wait_for(connector.close(), STOP_BOUND_SECS)
            except asyncio.TimeoutError:
                logger.warning("connected computers: a connector did not stop in time")
            except Exception:  # noqa: BLE001 -- the task is cancelled below either way
                logger.warning("connected computers: a connector stop failed", exc_info=True)
        if task is not None:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=STOP_BOUND_SECS)
            if not done:
                logger.warning("connected computers: a grant task did not end in time")
        self._status.pop(grant_id, None)

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(g) for g in list(self._tasks)), return_exceptions=True)

    async def shutdown(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        await self.stop_all()
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            await asyncio.gather(self._drain_task, return_exceptions=True)
        if self._lock is not None:
            # Only after every connector stopped, so the next host never overlaps.
            await asyncio.to_thread(self._lock.release)

    async def revoke(self, grant_id: str) -> Optional[Grant]:
        """Revoke one grant: persisted local refusal, bounded stop, then the remote."""
        grant = await asyncio.to_thread(self.store.revoke, grant_id)
        await self.stop(grant_id)
        self._kick_drain()
        return grant

    async def off(self) -> list[Grant]:
        """The local Off: refuse locally (persisted), stop (bounded), then tell remotes.

        Pending consents are dropped first, and the stored Off generation moves
        in the same write that revokes, so a native dialog still open or an SSH
        enrollment still running, in this process or another, writes no grant.
        The remote revokes run in the background after the stop.
        """
        from kiro_crew.connected_computers.local_routes import clear_consents

        clear_consents()
        ended = await asyncio.to_thread(self.store.revoke_all)
        await self.stop_all()
        self._kick_drain()
        return ended

    # -- queued remote revokes ------------------------------------------------------------
    def _kick_drain(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.ensure_future(self._drain())

    async def drain_pending(self) -> None:
        """Run (or join) one pass over the queued remote revokes."""
        self._kick_drain()
        assert self._drain_task is not None
        await asyncio.shield(self._drain_task)

    async def _drain(self) -> None:
        await drain_pending_forgets(
            self.store, self._instance, clock=self._clock, lock=self.pair_lock
        )

    # -- one grant ----------------------------------------------------------------
    def _still_active(self, grant_id: str) -> bool:
        """The switch and the grant, read from their stores: blocking, so off the loop."""
        if not self._enabled():
            return False
        grant = self.store.get(grant_id)
        return grant is not None and grant.active

    async def _renew(self, grant: Grant) -> Grant:
        """Renew over SSH, presenting the credential being replaced.

        The remote verifies it in full except its expiry, so a credential from
        before a remote logout or revoke never renews (:mod:`.enroll`).
        """
        from kiro_crew.connected_computers import enroll as enroll_mod

        instance = await asyncio.to_thread(self._instance, grant.instance_id)
        if instance is None:
            raise ConnectorRefused("the Remote Crew connection for this grant is gone")
        key = await asyncio.to_thread(self.store.install_key)
        name = await asyncio.to_thread(local_computer_name)
        enrolled = await enroll_mod.renew(
            instance,
            base=hostname_slug(name),
            fingerprint=install_fingerprint(key),
            label=name,
            credential=grant.credential,
        )
        if enrolled.gateway_id != grant.gateway_id or enrolled.device_id != grant.device_id:
            # The SSH target now answers as another gateway or computer: end.
            raise enroll_mod.EnrollmentFailed(
                "that gateway no longer matches the one you approved", "wrong_device"
            )
        renewed = await asyncio.to_thread(
            self.store.set_credential,
            grant.grant_id,
            enrolled.credential,
            enrolled.expires,
            enrolled.peer_key,
        )
        if renewed is None:
            raise ConnectorRefused("access to this computer was turned off on it")
        return renewed

    async def _end(self, grant_id: str, state: str) -> None:
        """A terminal outcome: shown as ``state``, never restarted for this grant.

        Persisted on the grant, so a restart of this host (or the other one)
        does not re-run the self-test or redial either. A write that fails is
        kept and retried by every reconcile; the grant stays stopped meanwhile
        and is shown as not yet saved.
        """
        self._status[grant_id] = state
        self._ended[grant_id] = state
        self._unsaved[grant_id] = state
        await self._save_ended()

    async def _save_ended(self) -> None:
        """Write every terminal state not yet on its grant."""
        for grant_id, state in list(self._unsaved.items()):
            try:
                await asyncio.to_thread(self.store.set_ended, grant_id, state)
            except Exception:  # noqa: BLE001 -- retried by the next reconcile
                logger.warning(
                    "connected computers: could not record an ended grant", exc_info=True
                )
                continue
            if self._unsaved.get(grant_id) == state:
                del self._unsaved[grant_id]

    async def _checked_plan(self, grant: Grant) -> Any:
        """The grant's confinement plan, self-tested once per controller life.

        Raises :class:`ConfinementUnavailable`; an ``OSError`` while preparing or
        spawning the self-test is translated into one with a safe reason.
        """
        plan = self._tested.get(grant.grant_id)
        if plan is not None:
            return plan
        self._status[grant.grant_id] = "checking confinement"
        try:
            # Off the loop: finding bwrap or sandbox-exec walks PATH and probes files.
            built: Any = await asyncio.to_thread(self._plan_factory)
            await self_test(built, grant.root)
        except OSError as exc:
            raise ConfinementUnavailable(
                f"the confinement self-test could not run ({type(exc).__name__})"
            ) from exc
        self._tested[grant.grant_id] = built
        return built

    def _renewal_due(self, grant: Grant) -> bool:
        """Less than :data:`RENEW_BEFORE_SECS` left by this clock, or none at all."""
        return grant.credential_expires - self._clock() < RENEW_BEFORE_SECS

    async def _run_grant(self, grant: Grant) -> None:
        backoff = BACKOFF_MIN_SECS
        #: The gateway's authenticated "expired": renew before the next dial,
        #: whatever this computer's clock says.
        renew_now = False
        try:
            plan = await self._checked_plan(grant)
        except ConfinementUnavailable as exc:
            await self._end(grant.grant_id, f"unavailable: {exc}")
            return
        while True:
            # Reading the grant is part of the attempt: a failed read waits with
            # the same back-off, so it never restarts this task (and its back-off).
            try:
                if not await asyncio.to_thread(self._still_active, grant.grant_id):
                    return
                current = await asyncio.to_thread(self.store.unsealed, grant.grant_id)
            except ProtectionUnavailable as exc:
                # The keyring may unlock later: wait, never connect without it.
                self._status[grant.grant_id] = f"unavailable: {exc}"
                await self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SECS)
                continue
            except SealBroken as exc:
                await self._end(grant.grant_id, f"refused: {exc}")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- VaultDocError and the like: retried
                self._status[grant.grant_id] = (
                    f"waiting: this computer's sharing state could not be read "
                    f"({type(exc).__name__})"
                )
                await self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SECS)
                continue
            if current is None or not current.active:
                return
            connector: Optional[Connector] = None
            try:
                if renew_now or self._renewal_due(current):
                    self._status[grant.grant_id] = "renewing access"
                    current = await self._renew(current)
                    renew_now = False
                instance = await asyncio.to_thread(self._instance, current.instance_id)
                port = getattr(instance, "local_port", 0) if instance is not None else 0
                if not port:
                    self._status[grant.grant_id] = "waiting for the Remote Crew connection"
                else:
                    connector = Connector(
                        current,
                        url=connect_url(port),
                        plan=plan,
                        grant_still_active=functools.partial(self._still_active, current.grant_id),
                    )
                    self._connectors[current.grant_id] = connector
                    self._status[grant.grant_id] = "connected"
                    why = await connector.run()
                    self._status[grant.grant_id] = f"disconnected: {why}"
            except AuthenticatedRefusal as exc:
                if exc.terminal:
                    await self._end(
                        grant.grant_id,
                        f"refused by the remote gateway ({exc.code}); share again",
                    )
                    return
                renew_now = renew_now or exc.expired
                self._status[grant.grant_id] = f"disconnected: {exc}"
            except EnrollmentFailed as exc:
                if exc.terminal:
                    # The remote, over the owner's SSH, said this access ended.
                    await self._end(grant.grant_id, f"refused by the remote gateway: {exc}")
                    return
                # SSH did not reach the remote, or it cannot renew now: retried.
                self._status[grant.grant_id] = f"disconnected: {exc}"
            except ConnectorRefused as exc:
                self._status[grant.grant_id] = f"refused: {exc}"
            except ProtectionUnavailable as exc:
                # A renewal could not be sealed; nothing was stored in the clear.
                self._status[grant.grant_id] = f"unavailable: {exc}"
            except SealBroken as exc:
                await self._end(grant.grant_id, f"refused: {exc}")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- reconnect with back-off
                if _is_remote_off(exc):
                    # The remote gateway has the feature off: not a refusal of
                    # this computer. Wait and retry; nothing local is revoked.
                    self._status[grant.grant_id] = (
                        "waiting: connected computers is off on the remote gateway"
                    )
                else:
                    # An upgrade answered 401/403 is as unauthenticated as a close
                    # code: a relay can send it. Retried, never terminal.
                    self._status[grant.grant_id] = f"disconnected: {type(exc).__name__}"
            finally:
                self._connectors.pop(grant.grant_id, None)
            if connector is not None and connector.admitted and not connector.protocol_failed:
                # Only an authenticated admission proves the gateway took this
                # computer: it alone resets the back-off.
                backoff = BACKOFF_MIN_SECS
            await self._sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SECS)


async def drain_pending_forgets(
    store: LocalGrantStore,
    instance_lookup: Callable[[str], Any],
    *,
    clock: Callable[[], float] = time.time,
    lock: Optional[asyncio.Lock] = None,
) -> int:
    """Tell each remote gateway whose revoke is due; returns how many confirmed.

    A confirmed revoke leaves the queue; any failure (unreachable, refused, the
    Remote Crew connection gone) is retried later with back-off. A grant is
    never touched here: it was revoked locally before it was queued.
    """
    from kiro_crew.connected_computers.enroll import forget

    confirmed = 0
    for entry in await asyncio.to_thread(store.pending_forgets):
        if entry.next_try > clock():
            continue
        async with lock if lock is not None else contextlib.nullcontext():
            # Re-read under the lock: a consent may have re-enrolled the pair.
            still = {p.key for p in await asyncio.to_thread(store.pending_forgets)}
            if entry.key not in still:
                continue
            instance = await asyncio.to_thread(instance_lookup, entry.instance_id)
            try:
                if instance is None:
                    raise LookupError("the Remote Crew connection is gone")
                await forget(instance, device_id=entry.device_id, fingerprint=entry.fingerprint)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- the local refusal already holds
                logger.info(
                    "connected computers: remote revoke not confirmed: %s", type(exc).__name__
                )
                await asyncio.to_thread(store.forget_failed, entry.key)
                continue
            await asyncio.to_thread(store.forget_done, entry.key)
            confirmed += 1
    return confirmed


def _is_remote_off(exc: BaseException) -> bool:
    """The upgrade answered :data:`REMOTE_OFF_STATUS`: the remote has the feature off."""
    return getattr(exc, "status", None) == REMOTE_OFF_STATUS


_CONTROLLER: Optional[LocalController] = None


def get_controller() -> LocalController:
    """This local gateway's controller (created on first use)."""
    global _CONTROLLER
    if _CONTROLLER is None:
        from kiro_crew.connected_computers.vault_doc import open_vault
        from kiro_crew.instances.registry import InstancesRegistry

        def lookup(instance_id: str) -> Any:
            return InstancesRegistry().get(instance_id)

        _CONTROLLER = LocalController(
            LocalGrantStore(open_vault()),
            instance_lookup=lookup,
            lock=ControllerLock.for_this_machine(),
        )
    return _CONTROLLER
