"""The two-tier self-heal of a tunnel, for ``kiro_crew.instances.ssh_tunnel_manager``.

The exit and healthy seams a child reports through, the recovery backoff, the
generation-gated rebuild, the recovered record write, and tracking and cancelling
the recovery tasks.

Each ``SshTunnelManager`` member here is a module-level function whose first parameter
is ``self``; the class in :mod:`kiro_crew.instances.ssh_tunnel_manager` binds it by
name, so the instance state, every patch of a method and every ``__new__`` build stay
on the one class.

Instance fields written: ``_recover_attempts``, ``_recovery_by_instance``,
``_recovery_tasks``, ``_tunnel_epoch``, ``_tunnels``.

Instance fields read: ``_hop_guard``, ``_lock``, ``_max_recovery``, ``_probe_fails``,
``_reconfiguring``, ``_recover_backoff_max``, ``_registry``, ``_ssh_compression``,
``_tunnel_factory``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace as dataclass_replace
from typing import TYPE_CHECKING

from kiro_crew.instances.constants import (
    DEFAULT_RECOVER_BACKOFF_MAX_SECS as _RECOVER_BACKOFF_MAX_SECS,
)
from kiro_crew.instances.registry import Instance, is_headless_provisioner
from kiro_crew.instances.ssh_tunnel_manager_child import TunnelState, _SshTunnel
from kiro_crew.instances.ssh_tunnel_manager_transport import _Mint, _TransportParams
from kiro_crew.instances.token_mint import TokenMintError
from kiro_crew.instances.validation import SshValidationError, SsmValidationError

if TYPE_CHECKING:
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

#: The manager's own logger: this code logs under the facade module's name.
logger = logging.getLogger("kiro_crew.instances.ssh_tunnel_manager")


# Self-heal respawn backoff: wait this base (doubled per consecutive attempt,
# capped) before rebuilding a failed tunnel, so a flapping link / bind race
# can't spin a tight respawn loop. Applied in the scheduling seam (_on_tunnel_exit)
# so direct _recover() callers (tests) aren't slowed.
_RECOVER_BACKOFF_BASE_SECS = 1.0


def _recover_backoff_secs(attempt: int, cap: float = _RECOVER_BACKOFF_MAX_SECS) -> float:
    """Exponential backoff before a self-heal rebuild, capped at *cap*. *attempt* is 1-based."""
    base = _RECOVER_BACKOFF_BASE_SECS * (2 ** max(0, attempt - 1))
    return min(base, cap)


class _RecoverySuperseded(Exception):
    """A self-heal rebuild found the tunnel generation moved mid-flight.

    Raised by :meth:`SshTunnelManager._rebuild` when its epoch-gated install
    is refused, and caught in :meth:`SshTunnelManager._recover`: the recovery
    must stand down entirely — falling through to the next tier instead would
    re-mint against a world the recovery has already been superseded in.
    """


# Named by the path callers import it from: a traceback and an error chain render a
# class as ``module.qualname``, so this module's name stays out of that text.
_RecoverySuperseded.__module__ = "kiro_crew.instances.ssh_tunnel_manager"


def _track_recovery(self: SshTunnelManager, instance_id: str, task: asyncio.Task) -> None:  # type: ignore[type-arg]
    """Retain a background task so it is not GC'd, indexed by instance."""
    self._recovery_tasks.add(task)
    per = self._recovery_by_instance.setdefault(instance_id, set())
    per.add(task)

    def _done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
        self._recovery_tasks.discard(t)
        bucket = self._recovery_by_instance.get(instance_id)
        if bucket is not None:
            bucket.discard(t)
            if not bucket:
                self._recovery_by_instance.pop(instance_id, None)

    task.add_done_callback(_done)


async def _cancel_recovery(self: SshTunnelManager, instance_id: str) -> None:
    """Cancel and AWAIT this instance's in-flight recovery.

    Self-heal reads the instance record before it takes the lock, so a
    recovery already in flight holds the PRE-edit coordinates. Letting it
    proceed would reinstall a tunnel to the old machine after the edit
    landed — and because ``connect()`` is idempotent, that tunnel would then
    be handed out for the new settings. Awaiting the cancellation is the
    point: returning while the task is still unwinding would leave exactly
    the race this closes.
    """
    tasks = list(self._recovery_by_instance.get(instance_id, ()))
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    # A cancelled recovery is expected to raise CancelledError; anything else
    # it raises is its own business and already logged by its done-callback.
    await asyncio.gather(*tasks, return_exceptions=True)


def _on_tunnel_healthy(self: SshTunnelManager, instance_id: str) -> None:
    """Sync seam invoked by a tunnel's probe loop on a proven live forward.

    A successful end-to-end probe is the only signal that clears the
    self-heal attempt counter. A rebuild's ``start()`` confirms only the
    LOCAL bind, so resetting the counter there would let a forward whose far
    end is dead rebuild "successfully" forever without ever reaching the
    recovery cap; and gating that reset on a single post-rebuild probe would
    instead ratchet the counter UP permanently whenever one 4s probe missed
    a still-booting far end. Clearing it here — on any interval whose probe
    traverses to the far end and back — makes the counter reflect observed
    end-to-end health: it climbs only while the forward stays dead across
    rebuilds (so ``_recover`` reaches ``_MAX_RECOVERY`` and hands off to
    diagnosis) and returns to zero the moment the forward answers again.
    Idempotent: dropping an absent key is a no-op, so a healthy tunnel that
    never needed recovery costs nothing.
    """
    self._recover_attempts.pop(instance_id, None)


def _on_tunnel_exit(self: SshTunnelManager, instance_id: str) -> None:
    """Sync seam invoked by a tunnel's monitor on unexpected exit.

    Takes the port back FIRST, synchronously, from the in-memory lease cache. This is
    the whole reason that cache exists: the child is already gone, so the lent port is
    free at the OS level from this instant, and any `await` before the bind -- even
    the thread hop of a registry read -- is a window in which another local process
    can take it and be handed the still-valid credential. A bind on loopback does not
    block, so nothing is gained by moving it off the loop and the ordering is what
    matters. The recovery task below then reconciles against the registry, which stays
    the source of truth.

    Schedules the async 2-tier recovery as a tracked task (refs retained so
    it isn't GC'd mid-flight; exceptions logged). A backoff (scaled by the
    consecutive-attempt count) is applied there, in the scheduling seam, so a
    flapping link / bind race can't spin a tight respawn loop — and so direct
    ``_recover`` callers (unit tests) aren't slowed.
    """
    self._hold_lent_port_now(instance_id)
    if instance_id in self._reconfiguring:
        # Its coordinates are being rewritten; whatever this recovery read
        # would already be stale. The reconfiguration tears the tunnel down
        # itself, and the user reconnects against the new record.
        logger.info("Skipping self-heal for %s: reconfiguration in progress", instance_id)
        return
    delay = _recover_backoff_secs(
        self._recover_attempts.get(instance_id, 0) + 1, self._recover_backoff_max
    )
    task = asyncio.create_task(self._recover_after(instance_id, delay))
    self._track_recovery(instance_id, task)
    task.add_done_callback(
        lambda t: (
            logger.error("Self-heal task crashed: %s", t.exception())
            if not t.cancelled() and t.exception()
            else None
        )
    )


async def _recover_after(self: SshTunnelManager, instance_id: str, delay: float) -> None:
    """Sleep *delay* (backoff) then run the 2-tier self-heal.

    Takes the hop holds FIRST, before the backoff and before any recovery attempt.
    The child is already gone by the time this runs, so its lent port is free at the
    OS level while the credential naming it stays valid -- and the rebuild that would
    re-occupy it is the whole backoff away, or never, once attempts run out. Waiting
    for the rebuild to close that window would leave it open for the backoff on every
    flap and permanently on the give-up path.
    """
    try:
        leases = await asyncio.to_thread(self._registry.live_hop_lease_deadlines)
        self._apply_hop_holds(leases)
    except Exception:
        logger.exception("Could not take hop holds after %s's forward exited", instance_id)
    if delay > 0:
        await asyncio.sleep(delay)
    if instance_id in self._reconfiguring:
        # Scheduled just before the barrier went up, or the barrier rose
        # during the backoff: either way this attempt holds stale coordinates.
        return
    try:
        await self._recover(instance_id)
    finally:
        # ONE re-settle covers every exit, and the in-use invariant is what makes
        # it correct in both directions: a rebuild that SUCCEEDED leaves the tunnel
        # CONNECTED, so its port counts as in use and the hold is dropped for the
        # live forward; a rebuild that failed or gave up leaves it in any other
        # state, so the port is not in use and the hold is taken. Enumerating the
        # recovery's failure exits instead would mean a new `return` silently
        # skipping it. `suppress(Exception)` deliberately does not catch
        # `CancelledError`, so a shutdown-cancelled recovery propagates rather than
        # awaiting inside a cancelled task.
        with contextlib.suppress(Exception):
            leases = await asyncio.to_thread(self._registry.live_hop_lease_deadlines)
            self._apply_hop_holds(leases)


async def _rebuild(
    self: SshTunnelManager,
    inst: Instance,
    params: _TransportParams,
    local_port: int,
    expected_epoch: int,
) -> _SshTunnel | None:
    """Build + start a fresh tunnel for *inst*, replacing the live one.

    Returns the installed tunnel when its start succeeds, else ``None`` —
    the object (not a bare bool) so the recovery can tell ITS install from
    one that raced in during the unlocked awaits (see _mark_recovered).

    Stops the existing tunnel first so its child is terminated and the
    local forward port is released before we spawn the replacement. Without
    this the old child orphans (dropped from ``_tunnels`` but never killed)
    and keeps holding the port, so every replacement fails
    ``ExitOnForwardFailure`` while ``_port_reachable`` is still satisfied by
    the orphan — the tight respawn loop this method otherwise produced.

    The install itself is gated, under the manager lock, on the stamp
    still reading *expected_epoch*: ``old.stop()`` is an unlocked await, so
    a concurrent ``connect()`` can install a live replacement (or a
    ``disconnect`` can end the generation) while it runs, and an ungated
    install would overwrite that replacement without stopping it — an
    orphaned child holding its port, untracked. On refusal the tunnel
    object is discarded unstarted (no process exists yet) and
    :class:`_RecoverySuperseded` is raised so the recovery stands down.
    Stopping ``old`` first is safe on every path: each caller reaches this
    method synchronously from the section that verified the stamp, so
    ``old`` is always the generation the recovery owns.
    """
    old = self._tunnels.get(inst.id)
    if old is not None:
        with contextlib.suppress(Exception):
            await old.stop()
    tunnel = self._tunnel_factory(
        inst.id,
        params.ssh_host,
        local_port,
        params.forward_remote_port(inst.remote_port),
        connect_timeout_secs=self._connect_timeout_for(params.method),
        compression=self._ssh_compression,
        probe_failure_threshold=self._probe_fails,
        on_exit=self._on_tunnel_exit,
        **params.tunnel_kwargs(),
    )
    tunnel.status.turn_url = params.turn_url(local_port)
    tunnel._on_healthy = self._on_tunnel_healthy
    async with self._lock:
        if self._tunnel_epoch.get(inst.id, 0) != expected_epoch:
            raise _RecoverySuperseded(inst.id)
        self._tunnels[inst.id] = tunnel
        # A self-heal reinstall is a new generation too: a mint in flight
        # against the tunnel this one replaces must not land on it.
        self._tunnel_epoch[inst.id] = expected_epoch + 1
    # Our OWN hold, if we have one, is on this port, so the bind below would lose to
    # it. Let go IMMEDIATELY before that bind and take it straight back unless the
    # bind succeeded -- which is what makes the unheld window exactly the bind it
    # exists for.
    #
    # Releasing any earlier, in the recovery's locked phase, would leave the port
    # unheld across tier 2's mint whenever tier 1's rebuild fails -- bounded by the
    # mint timeout rather than by a bind -- with the chained credential still naming
    # it. The caller's `finally` is no substitute: it runs only after the whole
    # two-tier recovery returns. Releasing here means a failed start re-takes the
    # port before returning, so tier 2 mints with it held.
    #
    # The `finally` is what covers every exit, including `_RecoverySuperseded` from the
    # revalidation below. A successful start needs no hold: the live forward owns the
    # port, `_apply_hop_holds` excludes it from `want`, and the re-arm here is a no-op
    # against a lease the cache does not name.
    self._hop_guard.release(local_port)
    owned = False
    try:
        started = await tunnel.start()
        # start() is an unlocked await with a window BEFORE the child spawns
        # (argv building). A disconnect or connect landing in that window
        # stops this tunnel while it has no process — a no-op — and then
        # untracks it, so the spawn lands afterwards with ours as the only
        # live handle. Revalidate under the lock and reap our own child when
        # displaced; after start() returns, any later displacer's stop() finds
        # the process and this window is closed.
        async with self._lock:
            superseded = (
                self._tunnels.get(inst.id) is not tunnel
                or self._tunnel_epoch.get(inst.id, 0) != expected_epoch + 1
            )
        if superseded:
            with contextlib.suppress(Exception):
                await tunnel.stop()
            raise _RecoverySuperseded(inst.id)
        # Only a start that SURVIVED the revalidation owns the port. The
        # superseded path above stops our own child, which frees the port again,
        # so it must fall through to the re-arm like any other failure.
        owned = bool(started)
        return tunnel if started else None
    finally:
        if not owned:
            self._hold_lent_port_by_number(local_port)


async def _mark_recovered(
    self: SshTunnelManager, instance_id: str, mine: _SshTunnel, expected_epoch: int
) -> None:
    """Record *mine* as the recovered tunnel iff its generation is current.

    ``mine.start()`` is an unlocked await, so the world can move between
    the gated install (see :meth:`_rebuild`) and this record: a
    ``connect()`` replaces *mine* (stopping it), or a ``disconnect`` tears
    it down — either way the record this write would make is about a
    tunnel that is not current, and persisting ``was_connected=True`` over
    a disconnect's ``False`` would silently revive, across restarts, an
    instance the user turned off. *expected_epoch* is the Phase 1 stamp
    plus the installs this recovery made itself; teardown bumps the stamp
    too, so any interleaved install OR teardown reads as a mismatch and
    the record is refused. Nothing needs unwinding on refusal: every
    displacing path stops the tunnel it displaces, so *mine* is already
    down and untracked.

    Persist the forwarder identity hints, under lock, iff still tracked.
    The attempt counter is NOT reset here — a bind-only rebuild does not
    prove the forward reaches its far end; the reset lives on a proven
    end-to-end probe success (see :meth:`_probe_loop`).

    A rebuild replaced the tunnel child, so the recorded forwarder
    identity (``forwarder_pid`` + ``forwarder_start`` + the
    ``local_port`` that child is bound to) must move with
    ``was_connected`` — a stale identity would point a later hard-kill
    reclaim at a process that does not exist (harmless, the identity
    check refuses it) while the ACTUAL replacement child leaked
    unrecorded. All hints go in one write.

    The record comes from :meth:`_forwarder_identity_hints`, so this write
    carries exactly the fields :meth:`connect`'s does and the pair cannot
    drift apart. ``local_port`` matters twice over here. A rebuild takes
    its port from the LIVE tunnel (see :meth:`_recover`), which may differ
    from the recorded one. And ``forwarder_sig`` is a MAC over the port, so
    recording a pid against a different port than the one it was signed
    with leaves the signature failing verification — the reclaim then
    refuses the very child this write exists to record, and every consumer
    reading the recorded port (the pane URL, :meth:`diagnose`'s fallback)
    addresses a port nothing is listening on.

    The persist stays INSIDE the manager lock so write order equals
    lock-acquisition order: a concurrent :meth:`disconnect`'s
    ``was_connected=False`` (also written under the lock) can never be
    overwritten by this recovery write landing late. The tracked-check
    gates the persist — an instance the user disconnected must not be
    re-marked auto-reconnectable. ``_persist_hint`` keeps the lock held
    until the worker write completes even under cancellation; a cancelled
    bare ``to_thread`` await would NOT stop the already-running thread, so
    its write could land after the lock released and break the ordering.
    """
    # A rebuild's start() waits only for the LOCAL forward to bind, so it
    # reports success even when the far end never answers. The attempt
    # counter is therefore NOT reset here: only a proven end-to-end probe
    # success clears it (see _probe_loop -> _on_tunnel_healthy). A rebuild
    # whose far end stays dead leaves the counter climbing, so _recover
    # reaches _MAX_RECOVERY and hands off to _schedule_diagnosis instead of
    # respawning a healthy-looking forward every interval; a rebuild whose
    # forward IS alive has its counter cleared by the next successful probe,
    # so a single slow probe cannot ratchet the budget down permanently.
    async with self._lock:
        tunnel = self._tunnels.get(instance_id)
        if tunnel is None:
            return
        if tunnel is not mine or self._tunnel_epoch.get(instance_id, 0) != expected_epoch:
            logger.info("Discarding a superseded self-heal rebuild for %s", instance_id)
            return
        await self._persist_hint(
            self._registry.update,
            instance_id,
            **await self._forwarder_identity_hints(instance_id, tunnel),
        )


async def _recover(self: SshTunnelManager, instance_id: str) -> None:
    """2-tier self-heal for an unhealthy tunnel (either transport).

    Tier 1: rebuild the tunnel (reusing the existing token).
    Tier 2: if rebuild fails, re-mint the token over the instance's
    transport, then rebuild. A fargate instance has no token, so its tier 2
    is a second rebuild.
    A CHAINED crew is minted for BEFORE tier 1, because only the mint reply
    names the port the parent's forward to this crew listens on, and tier 1's
    rebuild has to dial it; its tier 2 is therefore a second rebuild too.
    Capped at ``_MAX_RECOVERY`` consecutive attempts (reset when a rebuilt
    forward answers the end-to-end probe, via :meth:`_on_tunnel_healthy` — a
    bind-only rebuild does not zero it) so a persistently-broken host can't
    churn forever. No-ops if the instance was disconnected/removed or has
    already recovered while we waited for the lock.

    The slow remote I/O (mint; rebuild) runs **without** the manager lock —
    mirroring ``_refresh_token_once`` — so self-heal can't stall concurrent
    connect/disconnect/shutdown. The lock is held only for the
    validation/state checks and to store a freshly minted token.
    """
    # Phase 1 — validate + bump the attempt counter under the lock, then release.
    async with self._lock:
        inst = await asyncio.to_thread(self._registry.get, instance_id)
        current = self._tunnels.get(instance_id)
        if inst is None or current is None:
            return  # disconnected / removed while we waited
        if current.status.state == TunnelState.CONNECTED:
            self._recover_attempts.pop(instance_id, None)
            return  # already healthy (e.g. user reconnected)

        attempts = self._recover_attempts.get(instance_id, 0) + 1
        self._recover_attempts[instance_id] = attempts
        if attempts > self._max_recovery:
            logger.error(
                "Giving up self-heal for %s after %d attempts", instance_id, self._max_recovery
            )
            self._schedule_diagnosis(instance_id)
            return

        try:
            params = self._resolve_transport(inst, await self._with_parent(inst))
        except (SshValidationError, SsmValidationError) as e:
            logger.warning("Self-heal aborted for %s: %s", instance_id, e)
            return

        local_port = current.status.local_port or inst.local_port
        # The hold is NOT released here. Releasing it in this locked phase would leave
        # the port unheld across tier 2's mint whenever tier 1's rebuild fails, while
        # the chained credential still names it, and the caller's `finally` is no
        # substitute because it runs only after the whole two-tier recovery returns.
        # The release lives in `_rebuild`, immediately before the bind that needs it
        # and taken straight back when that bind does not produce a live forward, so
        # every slow step here -- the mint above all -- runs with the port held.
        # Which tunnel generation this recovery found. Nothing between here
        # and tier 1's rebuild installs a tunnel, so tier 2 can bind its
        # store to the ONE bump that a failed tier-1 rebuild is guaranteed
        # to make (see the store below). A chained crew's mint below does
        # await, so every step past this point re-reads the stamp under the
        # lock and stands down when it has moved -- the mint discards its
        # token and `_rebuild` raises _RecoverySuperseded.
        epoch = self._tunnel_epoch.get(instance_id, 0)

    # A chained crew's whole mint answer is taken at the top of this recovery
    # and carried here until the forward rides the hop it named.
    chained_mint: _Mint | None = None

    # Phase 2 — slow remote I/O WITHOUT the lock.
    # A CHAINED crew is minted for BEFORE tier 1 rebuilds -- the same
    # inversion `connect` makes, for the same reason: the forward's target
    # port must come from the parent, not from our row. A parent that came
    # back on a different loopback port is the commonest reason this crew
    # needs healing at all, and `ssh -L` binds the LOCAL side whatever
    # answers remotely, so `start()` -- which waits only for the local
    # forward to accept -- reports success against a hop that is gone. Tier 1
    # would then mark the crew recovered, reset the attempt counter, and
    # never reach the tier-2 mint that is the only place the parent names its
    # current port: healthy on the board, unreachable in fact, every cycle.
    # The mint rides the PARENT's hop, which is already up, so it runs before
    # this crew has any forward of its own.
    if params.is_chained:
        logger.info("Self-heal mint through parent for %s [attempt %d]", instance_id, attempts)
        try:
            mint = await self._mint_for(inst, params)
        except TokenMintError as e:
            # The hop runs through the parent, so a parent we cannot mint
            # over is a parent we cannot heal through either. Standing down
            # keeps the attempt counted, so _MAX_RECOVERY still ends in
            # diagnosis rather than a silent forever-retry.
            logger.warning("Self-heal mint through parent failed for %s: %s", instance_id, e)
            return
        hop = mint.hop
        if not hop:
            # Falling back to the row's port is the exposure `connect`
            # refuses: the row's copy arrived over a pane's postMessage, so a
            # forged notice would choose which loopback-only service on the
            # parent this gateway forwards and renders as a crew.
            logger.warning(
                "Self-heal aborted for %s: crew %r named no port for its forward to this crew",
                instance_id,
                params.via_instance_id,
            )
            return
        async with self._lock:
            if instance_id not in self._tunnels:
                return  # disconnected while minting -- discard
            if self._tunnel_epoch.get(instance_id, 0) != epoch:
                logger.info("Discarding a superseded self-heal mint for %s", instance_id)
                return
        if hop != params.via_remote_port:
            # The parent disagrees with the row. Its answer wins and is
            # persisted, so a later reconnect dials the same port this does.
            await self._persist_hint(self._registry.update, instance_id, via_remote_port=hop)
            inst = dataclass_replace(inst, via_remote_port=hop)
            params = dataclass_replace(params, via_remote_port=hop)
        # The token is NOT stored here, and that is the whole point of the
        # reorder: the forward still dials the hop the parent has just moved
        # off, so this crew's credential would be stored against a forward
        # that now reaches whichever crew the parent gave that port to. It is
        # carried to tier 1's rebuild and stored once the forward rides the
        # hop it was minted for -- the store refuses it before then anyway,
        # because the two ports disagree.
        chained_mint = mint

    # Tier 1 — rebuild tunnel, reuse existing token.
    logger.info("Self-heal tier 1 (rebuild tunnel) for %s [attempt %d]", instance_id, attempts)
    try:
        rebuilt = await self._rebuild(inst, params, local_port, expected_epoch=epoch)
    except _RecoverySuperseded:
        # A connect or disconnect moved the generation mid-rebuild. Not a
        # tier failure: falling through to tier 2 would re-mint against a
        # world this recovery has been superseded in.
        logger.info("Self-heal for %s stood down: superseded mid-rebuild", instance_id)
        return
    if rebuilt is not None:
        # The rebuild's install is the one bump expected past the Phase 1
        # stamp; anything else means the world moved mid-rebuild and
        # _mark_recovered unwinds instead of recording.
        if chained_mint is not None:
            # NOW the forward rides the hop this credential was minted for.
            async with self._lock:
                if not self._store_token(
                    inst, chained_mint, minted_at_epoch=epoch + 1, binds_forward=True
                ):
                    return
                self._schedule_token_refresh(instance_id)
        await self._mark_recovered(instance_id, rebuilt, epoch + 1)
        logger.info("Self-heal tier 1 finished for %s", instance_id)
        return

    # Tier 2 -- re-mint the dashboard token, then rebuild. A fargate instance
    # has no token, so its tier 2 is the rebuild alone: the tier-1 failure
    # already bumped the generation once, which is the stamp the second
    # rebuild binds to below. A chained crew's tier 2 is the rebuild alone
    # too, for the opposite reason -- its mint runs at the top of this
    # recovery, seconds ago, and carries the hop port tier 1 dialled.
    # A HEADLESS crew belongs on the re-forward arm, not the re-mint arm.
    # While this asked only about ``fargate``, a MicroVM crew took the else
    # branch, tried to mint, and ``_mint_for`` raised -- so the recovery stood
    # down and a dropped forward was never rebuilt. The connect path already
    # asks about the crew; this is the same question on the healing path.
    if params.method == "fargate" or params.headless or params.is_chained:
        logger.info("Self-heal tier 2 (re-forward) for %s", instance_id)
    else:
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the format names a token, its arguments never carry one
        logger.info("Self-heal tier 2 (re-mint token) for %s", instance_id)
        try:
            mint = await self._mint_for(inst, params)
        except TokenMintError as e:
            logger.warning("Self-heal re-mint failed for %s: %s", instance_id, e)
            return
        async with self._lock:
            if instance_id not in self._tunnels:
                return  # disconnected while minting -- discard
            # This mint ran for the generation tier 1 installed, which is
            # exactly ONE bump past the Phase 1 stamp: a failed tier-1 rebuild
            # always installs (and bumps for) its replacement before start()
            # reports failure, and every path that raises instead never
            # reaches this store. The store refuses any other value, and the
            # rebuild below would otherwise replace the current tunnel using
            # this recovery's stale record.
            if not self._store_token(inst, mint, minted_at_epoch=epoch + 1, binds_forward=False):
                return
            self._schedule_token_refresh(instance_id)
    try:
        rebuilt = await self._rebuild(inst, params, local_port, expected_epoch=epoch + 1)
    except _RecoverySuperseded:
        logger.info("Self-heal for %s stood down: superseded mid-rebuild", instance_id)
        return
    if rebuilt is not None:
        # epoch + 2: tier 1's failed install and this rebuild's own are
        # the two bumps this recovery made itself.
        if chained_mint is not None:
            # A chained crew's tier 2 mints nothing, so the answer still waiting
            # from the top of this recovery is published here -- against this
            # rebuild's generation, now that its forward rides the hop.
            async with self._lock:
                if not self._store_token(
                    inst, chained_mint, minted_at_epoch=epoch + 2, binds_forward=True
                ):
                    return
                self._schedule_token_refresh(instance_id)
        await self._mark_recovered(instance_id, rebuilt, epoch + 2)
        # Same rule as the connect path's mint, and for the same reason: a
        # headless crew has no dashboard session to prime, whatever transport
        # reaches it. Leaving this one keyed on the transport would put the
        # self-heal path on the transport while the connect path asks about
        # the crew. This seam is owned elsewhere, so the change here is one
        # condition.
        if params.method != "fargate" and not is_headless_provisioner(inst.provisioner_id):
            await self._prime_peer_session(instance_id)
        logger.info("Self-heal tier 2 finished for %s", instance_id)
    else:
        logger.warning("Self-heal failed for %s even after re-mint", instance_id)
