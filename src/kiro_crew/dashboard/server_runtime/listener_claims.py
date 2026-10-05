"""The credentials a gateway publishes per listener, and the guards that keep them true.

The secret files, the listener sidecars recorded for each bound address, the Windows
listener guards with their reconciliation against the live socket, and the
listener-lost exit.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        LISTENER_LOST_EXIT_CODE,
        DashboardState,
        ListenerGuard,
        SecondaryLoopback,
        _bind_once,
        logger,
        platform_compat,
        port_resolution,
        run_marker,
        shutdown_event,
    )


def _live_sibling_port(own_port: int) -> int | None:
    """A DIFFERENT port in this data home whose gateway is verifiably alive.

    ``None`` when this start is the only live gateway in the home, which is the
    normal single-instance case. Uses the same ownership proof the client port
    discovery already trusts (recorded pid, actually holds the port, same uid,
    argv looks like a gateway), so a stale marker left by a crash does not count
    as a sibling and never blocks a legitimate credential write.

    Blocking (/proc + filesystem); call from the executor, never the loop.
    """
    try:
        for port in run_marker.marker_ports():
            if int(port) == int(own_port):
                continue
            if port_resolution._gateway_owns_port(int(port)):
                return int(port)
    except Exception:
        # Discovery failing must not block startup: fall through to the write.
        # A missed sibling degrades to the pre-existing last-writer-wins
        # behaviour, never to a gateway that cannot start.
        logger.debug("live-sibling discovery failed", exc_info=True)
    return None


def _write_instance_credentials(
    secret_path: Path,
    port: int,
    host: str,
    secret: str,
    extra_hosts: Sequence[str] = (),
) -> None:
    """Publish this gateway's internal-API credential.

    Writes up to three files with different lifetimes:

    * ``run/gateway-<port>.secret`` -- ALWAYS, and FIRST. Paired with the port
      rather than the listener, for readers that resolve a port and nothing
      finer. First because it is load-bearing for boot: a pod waits on it.
    * ``run/gateway-<port>-<address>.secret`` -- whenever the bound address is
      known. Names ONE listener, so a client that dialled a specific address
      either reads the credential of the party it reached or reads nothing. A
      port number alone cannot carry that: ``KIROCREW_BIND=::1`` leaves IPv4
      ``127.0.0.1:<port>`` free for a co-resident to take, and a port-keyed
      lookup would hand that co-resident this gateway's credential.
    * ``.local_secret`` -- only when no other gateway in this data home is
      verifiably alive on a different port. Overwriting it while a sibling is
      serving is the desync this guard exists to prevent: the sibling keeps
      comparing against its own in-memory value, every internal caller then
      sends the newcomer's credential, and the whole internal channel answers
      403 with a bare ``Forbidden`` until one of them restarts. The shared file
      is still written in the single-instance case because pre-per-port clients
      (an older CLI, a cron script from a previous install) read only that path.

    The listener-keyed write is CONTAINED rather than fatal, and it is ordered
    after the credential a booting pod waits on. ``_write_secret_file`` raises
    ``OSError`` on any failure -- including a Windows DACL apply that cannot
    resolve the invoking SID -- and the caller answers an ``OSError`` here by
    tearing the runner down, so letting this one propagate would let an extra
    artifact stop the gateway from starting at all. Its absence is safe in a way
    that is not true of the others: a client that finds no entry for the address
    it dialled refuses and asks for a token, so the cost is one explicit
    sign-in.

    An empty *host* suppresses the listener-keyed write for the same reason
    rather than filing the credential under a guessed address.

    Blocking fs I/O; the caller offloads this whole function.
    """
    _write_secret_file(run_marker.secret_path(int(port)), secret)
    # One sidecar per address this generation actually bound. The SET of them is
    # what a client reads to answer "does this gateway hold every family the host
    # I am dialling can resolve to?" -- so a gateway holding both loopback
    # families publishes two, and a name that resolves to either reaches only
    # this gateway. A single-family gateway publishes one, and a client dialling
    # the name finds a family uncovered and signs in explicitly instead.
    for address in dict.fromkeys(a for a in (host, *extra_hosts) if a):
        listener_path = run_marker.listener_secret_path(int(port), address)
        try:
            _write_secret_file(listener_path, secret)
        except OSError:
            # Named, not silent: a client dialling this address falls through to
            # the sign-in prompt, and the operator should be able to see why.
            # Only the file NAME is logged, never a value read from it.
            logger.warning(
                "Could not publish the listener sidecar %s; clients dialling that "
                "address will sign in explicitly instead.",
                listener_path.name,
                exc_info=True,
            )
        else:
            # Recorded only on success, so shutdown deletes exactly what this
            # generation put on disk and never a sibling's entry (see
            # run_marker.clear_marker).
            run_marker.note_published_listener(int(port), address)
    sibling = _live_sibling_port(int(port))
    if sibling is not None:
        logger.warning(
            "Not overwriting %s: another gateway in this data home is live on port %d. "
            "This instance's credential is published as %s; clients that resolve port %d "
            "will authenticate against it.",
            secret_path,
            sibling,
            run_marker.secret_path(int(port)).name,
            port,
        )
        return
    _write_secret_file(secret_path, secret)


def _write_secret_file(secret_path: Path, secret: str) -> None:
    """Write *secret* to *secret_path* with mode 0o600.

    Creates the parent directory if needed. On failure the (possibly
    truncated) file is removed and the original ``OSError`` is re-raised.
    Caller is responsible for any further cleanup (e.g. tearing down the app
    runner). Both blocking steps (``mkdir`` and the ``os.open``/``os.close`` +
    ``restrict_to_owner`` write) live here so the caller can offload the whole
    thing with a single ``run_in_executor`` (no-blocking-call-on-event-loop).
    """
    try:
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(secret_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            # Enforce perms even if the file already exists at looser mode.
            # restrict_to_owner (fail-loud), NOT fchmod_safe: fchmod_safe swallows
            # OSError, which would defeat the cleanup-and-reraise below — a
            # pre-existing file with loose perms would stay loose and the caller
            # never learns. On POSIX this applies chmod 0o600 by path;
            # on Windows an owner-only DACL (fchmod doesn't exist on
            # Windows, where a raw fchmod would be a silent no-op).
            platform_compat.restrict_to_owner(secret_path)
            with os.fdopen(fd, "w") as f:
                fd = -1  # fdopen took ownership; skip the redundant close below
                f.write(secret)
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
    except OSError:
        try:
            secret_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _register_listener_guard_shutdown(app: web.Application, state: DashboardState) -> None:
    """Register the on_cleanup hook that detaches the listener guard(s).

    MUST be called BEFORE ``runner.setup()`` freezes the app's signal lists. The
    guard is created after the TCP site binds (:func:`_arm_listener_guard`) and
    resolved here lazily via ``getattr``. Detaching first matters: cleanup stops
    every site, and a guard still armed would read its own site's closed
    listener as a lost one and try to rebind it mid-shutdown.

    Both guards are detached, SECONDARY FIRST, because that is the reverse of the
    order they were armed and the order is load-bearing. ``arm()`` captures the
    handler it displaced and delegates to it, so the secondary guard sits in
    front of the primary, and each guard restores its neighbour only while it is
    still the installed handler. Detaching the primary first therefore restores
    nothing and leaves the secondary's handler installed on the loop for good.
    """

    async def _listener_guard_shutdown(app_: web.Application) -> None:
        for attr in ("_secondary_listener_guard", "_listener_guard"):
            guard = getattr(state, attr, None)
            if guard is not None:
                guard.stop()

    app.on_cleanup.append(_listener_guard_shutdown)


def _arm_listener_guard(
    state: DashboardState, runner: web.AppRunner, site: web.TCPSite | web.SockSite
) -> None:
    """Watch the just-started *site* and rebind it if its listener dies.

    Windows only, because the defect is: one failed ``accept()``
    (``ERROR_NETNAME_DELETED`` from an aborted tunnelled peer) makes the
    proactor loop close the LISTEN socket for good while the process and its
    accepted connections live on. The guard hooks the loop's exception handler
    for that exact report, self-probes ``/api/live`` over loopback
    periodically, rebinds the same host/port with bounded backoff, and exits
    non-zero when it cannot -- see
    :mod:`kiro_crew.dashboard.listener_guard`. Shared by ``start_dashboard``
    and the headless ``start_api_server``. POSIX selector loops keep the
    listener registered across a failed accept, so on those platforms this is
    a no-op rather than an idle probe task.
    """
    if not platform_compat.IS_WINDOWS:
        return

    # The guard's recovery contract is "rebind the listener's REAL bound name"
    # (see ``ListenerGuard.__init__``): it captures host/port from the live
    # LISTEN socket at construction. A site with no live asyncio server — an
    # inert site double in wiring tests, or a site whose ``start()`` was faked
    # out — gives the guard no name to capture and no listener to probe, and
    # arming it would misread boot as a dead listener. Real sites cannot hit
    # this: both callers arm immediately after ``await site.start()``, which
    # is what creates ``_server`` and its sockets.
    if not getattr(getattr(site, "_server", None), "sockets", None):
        return

    guard = ListenerGuard(
        runner,
        site,
        shutdown_event,
        bind_factory=_bind_once,
        # The primary's listener-keyed sidecar is subject to the same invariant
        # as the secondary's: it claims this gateway holds that address NOW, and
        # a rebind window is a stretch of time when nobody holds it. The
        # port-keyed credential is deliberately untouched -- it names the
        # generation, not a listener, and a booting pod waits on it.
        on_listener_lost=lambda: _withdraw_listener_sidecar(state, "primary"),
        on_listener_restored=lambda: _republish_listener_sidecar(state, "primary"),
    )
    guard.arm()
    state._listener_guard = guard


def _withdraw_listener_sidecar(state: DashboardState, which: str) -> bool:
    """Stop advertising one listener's address while this gateway does not hold it.

    Returns whether the address is unadvertised, which is a fact about the FILE
    rather than about this call: a claim that was never recorded advertises
    nothing, so it answers True. False means the sidecar could neither be
    removed nor blanked, so the credential is still readable for an address this
    generation does not hold -- the one outcome a caller must not treat as
    cleanup, because a co-resident that takes the address receives whatever a
    client sends to the name.
    """
    claim = getattr(state, "_listener_sidecars", {}).get(which)
    if claim is None:
        return True
    port, address, _secret = claim
    if run_marker.withdraw_published_listener(port, address):
        logger.warning(
            "Withdrew the %s listener sidecar for [%s]:%d: this gateway no longer holds "
            "that address, so clients dialling a name that resolves there will sign in "
            "explicitly instead of sending a credential to whoever takes it.",
            which,
            address,
            port,
        )
        return True
    # A False from the retraction has TWO causes that must not be collapsed: the
    # filesystem refused, and there was nothing of ours to retract (an address
    # this process never published, or one a previous call already withdrew --
    # the give-up path repeats the withdrawal by design). Only the first is an
    # advertised credential, so the answer is read off the file rather than off
    # the call: absent or empty covers no family, and a reader skips a blank
    # secret. An unreadable file is the refusal case and answers False.
    try:
        sidecar = run_marker.listener_secret_path(port, address)
        return not sidecar.exists() or sidecar.stat().st_size == 0
    except OSError:
        return False


def _republish_listener_sidecar(state: DashboardState, which: str) -> None:
    """Re-advertise a listener's address after a rebind has actually bound it."""
    claim = getattr(state, "_listener_sidecars", {}).get(which)
    if claim is None:
        return
    port, address, secret = claim
    if not secret:
        return
    try:
        _write_secret_file(run_marker.listener_secret_path(port, address), secret)
    except OSError:
        logger.warning(
            "Could not re-publish the %s listener sidecar for [%s]:%d after a rebind; "
            "clients dialling that address will sign in explicitly.",
            which,
            address,
            port,
            exc_info=True,
        )
        return
    run_marker.note_published_listener(port, address)


def _note_listener_sidecar(
    state: DashboardState, which: str, port: int, address: str, secret: str
) -> None:
    """Tell the guards which sidecar each listener owns, so they can maintain it.

    Called AFTER publication, because a claim that was never written must not be
    withdrawn or re-published. Recorded on *state* rather than captured in the
    guard's closure because the guard is armed at bind time, before the resolved
    port and the bound address are known -- the hooks resolve the claim when they
    fire instead.

    The credential rides along, and that is not a second copy of it: ``secret`` is
    the same immutable ``str`` object already held as ``app["local_secret"]`` for
    the auth middleware to compare against, so the process's exposure is
    unchanged. The alternative -- reading the value back off the port-keyed
    sidecar at re-publication time -- would make a rebind trust a file instead of
    the value this generation minted.
    """
    if not address:
        return
    sidecars = getattr(state, "_listener_sidecars", None)
    if sidecars is None:
        sidecars = {}
        state._listener_sidecars = sidecars
    sidecars[which] = (int(port), address, secret)


def _arm_secondary_listener_guard(
    state: DashboardState,
    runner: web.AppRunner,
    secondary: SecondaryLoopback,
    port: int,
) -> None:
    """Watch the SECOND loopback listener and stop advertising it if it dies.

    The primary listener gets :func:`_arm_listener_guard`; this one needs its own
    guard for the same Windows defect and a different terminal action. One failed
    ``accept()`` closes a LISTEN socket for good while the process lives on, so
    without this the second family's socket can be gone while its sidecar still
    tells every client that this gateway holds that address -- and a co-resident
    that then binds the freed address receives the credential a client sends to
    the name. The sidecar's whole meaning is presence, so an unguarded second
    listener makes the claim unfalsifiable.

    Two differences from the primary, both deliberate:

    * **Its own attribute.** ``ListenerGuard`` keeps a reference to the handler it
      displaced and delegates to it, so two guards chain correctly -- but
      ``state._listener_guard`` is a single slot and reusing it would drop the
      primary's guard on the floor. Detachment then has to run in the REVERSE of
      the arming order (see :func:`_register_listener_guard_shutdown`), or the
      inner guard stays installed on the loop.
    * **It never exits the process.** The primary's terminal action is right for
      the listener a gateway exists to serve. Losing an ADDITIONAL family costs a
      client dialling a name one explicit sign-in, so killing a gateway that is
      still serving its primary listener would turn a degradation into an outage.
      The injected action withdraws the sidecar and stops guarding, which lands
      exactly in the degraded state this feature already handles and tests: a
      family uncovered, so clients refuse to send and prompt instead.
    """
    if not platform_compat.IS_WINDOWS:
        return
    if not getattr(getattr(secondary.site, "_server", None), "sockets", None):
        return
    guard = ListenerGuard(
        runner,
        secondary.site,
        shutdown_event,
        bind_factory=_bind_once,
        on_listener_lost=lambda: _withdraw_listener_sidecar(state, "secondary"),
        on_listener_restored=lambda: _republish_listener_sidecar(state, "secondary"),
        on_give_up=lambda reason: _secondary_listener_given_up(state, port, secondary, reason),
    )
    guard.arm()
    state._secondary_listener_guard = guard


def _reconcile_listener_publication(
    state: DashboardState, which: str, port: int, address: str
) -> None:
    """Check a just-published listener against its live socket, once.

    Closes the window neither half can see on its own. Publication is an executor
    await, and a Windows accept failure landing inside it leaves a published claim
    for a closed listener: the guard either is not armed yet (the second family) or
    is armed with no claim recorded yet (the primary), and a withdrawal with no
    recorded claim answers True without touching the file, which recovery reads as
    "proceed". Either way the sidecar keeps naming an address this gateway does not
    hold, and nothing revisits it -- the probe is 60 seconds away and a rebind only
    helps if it binds.

    So EVERY published claim is reconciled the moment it is recorded, on every
    startup path. ``listener_open`` is the same test the guard's probe uses, so this
    asks the probe's question early rather than a different question, and a closed
    socket goes to ``check_now`` -- the guard's own entry point -- so the remedy is
    withdraw, rebind, escalate, not a second implementation of them here.

    Scheduled as a task because recovery is async and boot must not wait on a
    rebind; the guard holds every decision that follows. An unarmed guard (every
    POSIX platform, where the defect does not exist) has nothing to reconcile.
    """
    attr = "_listener_guard" if which == "primary" else "_secondary_listener_guard"
    guard = getattr(state, attr, None)
    if guard is None or guard.listener_open():
        return
    logger.critical(
        "The %s listener on [%s]:%d was already closed when its credential finished "
        "publishing, so the sidecar named an address this gateway does not hold; "
        "reconciling now instead of waiting for the probe",
        which,
        address,
        port,
    )
    task = asyncio.create_task(guard.check_now("closed during credential publication"))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def _request_listener_lost_exit(state: DashboardState, reason: str) -> None:
    """Ask every armed guard for the listener-lost exit, so the process ends.

    Both guards, because the exit status is read off ONE of them
    (``_listener_guard``) while the condition can be discovered by the other, and
    a request that lands only on the discoverer would set the shutdown event with
    an exit status of 0 -- a clean stop, which is what a supervisor does NOT
    relaunch. Missing guards are skipped rather than required: neither arms off
    Windows, and the caller reaches here only from a guard that did.
    """
    for attr in ("_listener_guard", "_secondary_listener_guard"):
        guard = getattr(state, attr, None)
        if guard is not None:
            guard.request_exit(reason)


def _secondary_listener_given_up(
    state: DashboardState, port: int, secondary: SecondaryLoopback, reason: str
) -> None:
    """Terminal state for the second family: uncovered, and the gateway serves on.

    ``on_listener_lost`` has already withdrawn the sidecar by the time a give-up
    is reached, so this is idempotent by construction -- it repeats the
    withdrawal because the guard also gives up on the path where a rebind BOUND
    the address and the listener still answers nothing, and on that path the
    address was re-advertised in between.

    Serving on is right only while the withdrawal LANDED. A sidecar that can be
    neither removed nor blanked keeps advertising a live credential for an address
    this gateway has stopped holding, and no later pass revisits it: the guard is
    done, and ``clear_marker`` uses the same filesystem that just refused. That is
    not the one-explicit-sign-in degradation this terminal action exists for, so
    it escalates to the primary's action -- ending the process, which is what
    makes the readable value stop authenticating anywhere.
    """
    if not _withdraw_listener_sidecar(state, "secondary"):
        logger.critical(
            "The second loopback listener on [%s]:%d is gone (%s) and its sidecar could "
            "not be withdrawn, so a live credential stays readable for an address this "
            "gateway no longer holds; exiting with status %d instead of serving on "
            "behind a claim nothing can retract",
            secondary.address,
            port,
            reason,
            LISTENER_LOST_EXIT_CODE,
        )
        _request_listener_lost_exit(state, reason)
        return
    logger.warning(
        "The second loopback listener on [%s]:%d is not coming back (%s). The gateway "
        "keeps serving its primary listener; clients dialling a name that resolves to "
        "[%s] will sign in explicitly.",
        secondary.address,
        port,
        reason,
        secondary.address,
    )
