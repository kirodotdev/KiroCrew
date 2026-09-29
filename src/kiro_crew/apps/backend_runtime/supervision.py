"""A backend's health as a standing observation: the startup poll and the liveness watch.

One daemon thread per record, bound to that record from the spawn: it polls until the
backend first answers, then keeps watching at the coarser ``_HEALTH_WATCH_INTERVAL`` for
as long as the SAME record stays tracked. Each sweep re-reads the execution ceiling
before it judges health, demotes a live process only after consecutive misses, treats an
exited spawned process as decisive, and hands it to the restart supervisor once its MCP
scrub has landed.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Literal

from kiro_crew.apps.backend_runtime import _FACADE
from kiro_crew.apps.backend_runtime.ports import _capture_adopted_owners
from kiro_crew.apps.backend_runtime.probe import (
    HealthProbeOutcome,
    _health_failure_hint,
    _health_probe,
)
from kiro_crew.apps.backend_runtime.registration import (
    _demote,
    _promote,
    _retry_mcp_reconcile,
    _set_backend_health,
)
from kiro_crew.apps.backend_runtime.restart import _restart_exited_backend
from kiro_crew.apps.backend_runtime.termination import stop_app_backend
from kiro_crew.apps.backend_runtime.tracking import (
    AppProcess,
    _health_reconcile_lock,
    _lock,
    _processes,
    _restart_attempts,
)
from kiro_crew.apps.execution import app_execution_denied, third_party_ceiling_closed

logger = logging.getLogger(_FACADE)


_HEALTH_CHECK_RETRIES = 15
_HEALTH_CHECK_INTERVAL = 2.0

# Post-startup liveness watch (see _watch_backend_health). The startup poll above only
# establishes that a backend CAME UP; without a standing watch `healthy` would be a
# write-once cache and a backend that died later would keep the reverse proxy routing to
# a dead port. The interval is far coarser than the startup one because it runs for the
# whole life of every backend, and nothing is waiting on it — a demotion that lands one
# interval late costs a few refused requests, whereas a tight poll costs one HTTP round
# trip per backend forever.
_HEALTH_WATCH_INTERVAL = 15.0
# Consecutive failed probes of a process that is still ALIVE before it is demoted. A
# backend can be briefly busy (a slow request, a GC pause), so demoting on a single miss
# would take a working app offline; an exited process needs no threshold at all because
# it cannot recover. See _watch_backend_health.
_HEALTH_WATCH_FAILURES = 3
# Consecutive successful liveness sweeps before a replacement is considered stable enough
# to reset its crash-restart budget. A startup health gate proves only that the backend
# came up; this window prevents a post-gate flapper from restarting forever at 1s.
_RESTART_STABLE_SWEEPS = 4


def _health_check_loop(ap: AppProcess, health_path: str) -> AppProcess | None:
    """Poll the health endpoint until it responds or we give up.

    Takes the RECORD rather than a name and a port, which is what binds the whole poll to
    one generation. A name plus a port are two independent inputs that can disagree: a
    stop/start landing between the spawn and this thread's first statement would hand a
    name lookup the SUCCESSOR while the port argument still named the predecessor, and
    the poll would then promote — or on exhaustion scrub — a backend whose port it never
    probed. Deriving both from the record leaves nothing to disagree.

    Returns the record if this call promoted it to healthy, else None (never answered, or
    not the tracked entry).
    """
    app_name = ap.app_name
    port = ap.port
    last = HealthProbeOutcome(None, "no probe completed")
    for attempt in range(_HEALTH_CHECK_RETRIES):
        time.sleep(_HEALTH_CHECK_INTERVAL)
        with _lock:
            if _processes.get(app_name) is not ap:
                return None  # replaced or stopped — this poll is a retired generation
        # The ceiling is re-read HERE too, not only by the standing watch. This poll owns
        # the record for up to `_HEALTH_CHECK_RETRIES * _HEALTH_CHECK_INTERVAL` seconds,
        # and the watch does not take over until it ends and then sleeps its own first
        # interval. An operator closing the ceiling during an app's startup is an
        # ordinary race, and without this the spawn would keep polling and could still
        # PROMOTE — which is what writes the app's url into mcp.json — under a ceiling
        # that is already closed.
        #
        # The same call the watch makes, on the same record, so the population rule and
        # the builtin exemption cannot read differently on the two paths. Any verdict but
        # `proceed` abandons the promotion; `_supervise_backend_health` then hands a
        # still-tracked record to the watch, which keeps retrying a refused stop.
        if _revoke_if_ceiling_closed(ap, health_path) != "proceed":
            return None
        last = _health_probe(port, health_path)
        if last.healthy:
            # Health-gated MCP registration: only now that the
            # backend has passed /health do we write its HTTP MCP url (live port) to
            # global mcp.json. Registering before this could leave a dead-but-enabled
            # url for an app whose backend never became healthy — the kiro-cli outage.
            # Routed through the shared transition, which re-checks that `ap` is still
            # the tracked record, so this is ordered against the watch's demotions
            # rather than racing them.
            if not _set_backend_health(ap, healthy=True):
                return None
            logger.info(
                "App %s backend healthy (port %d, attempt %d)",
                app_name,
                port,
                attempt + 1,
            )
            return ap

    logger.warning(
        "App %s backend failed health check after %d attempts (last: %s)%s",
        app_name,
        _HEALTH_CHECK_RETRIES,
        last.detail,
        _health_failure_hint(last),
    )
    # Backend never became healthy: scrub any optimistic/stale MCP entry so kiro-cli does
    # not keep dialing a dead port on every session (the reverted-outage shape).
    #
    # Identity-guarded like every other transition, on the one record this loop probed.
    # `_deregister_mcp_servers` removes by app NAME, so a restart during the startup
    # window — whose successor may already have come up and registered — would otherwise
    # have this retiring loop scrub the healthy successor's entry. That scrub also
    # bypasses the record, so the successor's `mcp_healthy` would still read True and the
    # watch's retry condition would never fire to put it back.
    _set_backend_health(ap, healthy=False)
    return None


def _rebind_adopted_owners(ap: AppProcess, health_path: str) -> bool:
    """Re-capture an adopted backend's owning PIDs, or refuse to confirm ownership.

    Reuses the adoption-time consistency sandwich (:func:`_capture_adopted_owners`), so a
    responder that exits mid-capture cannot hand ownership to a bystander. Returns False
    when ownership cannot be established, which the caller treats as "do not promote".
    """
    try:
        captured = _capture_adopted_owners(ap.app_name, ap.port, health_path)
    except Exception as exc:  # noqa: BLE001 — the watch must never die on a probe
        logger.warning(
            "App %s: could not re-capture adopted owners on port %d: %s",
            ap.app_name,
            ap.port,
            exc,
        )
        return False
    if captured is None:
        logger.warning(
            "App %s: adopted backend on port %d answered again but its ownership could "
            "not be confirmed — leaving it unhealthy",
            ap.app_name,
            ap.port,
        )
        return False
    pids, start_times = captured
    with _lock:
        if _processes.get(ap.app_name) is not ap:
            return False
        ap.adopted_pids = pids
        ap.adopted_start_times = start_times
    return True


def _watch_backend_health(ap: AppProcess, health_path: str) -> None:
    """Run the liveness watch, surviving an unexpected fault in any single sweep.

    The whole point of the guard is the failure MODE: an exception escaping the sweep
    kills this daemon thread, and a dead watch freezes ``healthy`` at its last value —
    silently restoring the write-once behaviour this watch exists to remove, with the
    proxy still routing to a port nothing serves. A logged fault that costs one sweep is
    strictly better. Restarting resets the consecutive-failure counter, which is the
    conservative direction (it delays a demotion rather than causing a spurious one), and
    the inner loop sleeps before its first probe, so a persistent fault cannot spin.
    """
    while True:
        try:
            _watch_backend_health_sweeps(ap, health_path)
            return
        except Exception:  # noqa: BLE001 — see the docstring; a dying watch is the bug
            logger.warning(
                "App %s: health watch sweep failed on port %d; restarting the watch",
                ap.app_name,
                ap.port,
                exc_info=True,
            )
            with _lock:
                if _processes.get(ap.app_name) is not ap:
                    return  # not the tracked record — nothing left to watch


def _revoke_if_ceiling_closed(
    ap: AppProcess, health_path: str
) -> Literal["stopped", "retry", "proceed"]:
    """Stop *ap* when its app is not admitted to execute.

    ``stopped`` - it is gone; the watch is done. ``retry`` - the ceiling is closed
    and the stop did not take, so the caller must SKIP its health judgement this
    sweep (see the promotion hazard where that is returned). ``proceed`` - nothing
    was revoked, so the sweep carries on: the ceiling does not apply, or a fault
    left the question unanswered and the liveness watch must keep working.

    Turning ``agent.apps_allow_third_party`` off has to stop the code it was
    admitting, and the setting has three writers: the dashboard endpoint, which
    sweeps on the falling edge; the CLI; and a text editor. Only the endpoint
    sweeps, and the boot reconcile in :func:`start_enabled_app_backends` revokes
    at the NEXT start, so without this a backend admitted solely by the blanket
    flag keeps serving under a ceiling the operator has closed: trust withdrawn
    on paper only, the one failure this control exists to prevent.

    This watch is the only thing that already revisits every live backend, so
    enforcing here adds no task, no interval, and no setting: a closed ceiling
    stops the process within one sweep whatever route closed it.

    Scope is the EXECUTING surface and stops there. An app holding its own
    ``agent.apps_trusted`` grant is untouched: that permission is independent of
    the blanket flag. Builtins are exempt at the gate on shipped provenance.
    Non-executable derivative resources (agents, skills, MCP declarations, cron
    definitions) sit outside the ceiling by contract and belong to the lifecycle
    lock's owners; a cron or hook that tries to RUN app code meets
    ``app_execution_denied`` and fails closed on its own.

    No ``on_shutdown`` hook is attempted, and that is not an oversight. The flag
    is already false by the time this observes it, so ``load_app_module`` refuses
    to load the hook, which is the state the endpoint's post-write second pass
    also runs in. Pretending to run it would report a teardown that cannot happen.
    This is why the endpoint remains the better route for withdrawing trust.

    Never raises: the enclosing sweep is wrapped, but a fault here costs the
    liveness watch that other code depends on, so a failed revocation is logged
    and retried on the next sweep instead.
    """
    name = ap.app_name
    try:
        # POPULATION: records the gateway itself created. Nothing the app writes can
        # move it out of scope, which is the point. Reading the app's
        # ``installed.json`` to decide whether the ceiling applies let an app trusted
        # to run code delete its own metadata and be skipped by the sweep that exists
        # to stop it. A record the gateway did not create carries no claim about a
        # process the gateway started, so it is left alone.
        if not ap.gateway_started:
            return "proceed"
        # Shipped code is exempt at the gate, and the classification was made once on
        # the execution target the gate vetted. Reading it here rather than re-resolving
        # anything is what closes the two ways the exemption was forgeable: `origin` in
        # `installed.json` is written by the app, and a stored PATH is resolved against a
        # filesystem the app owns, so an entry point replaced by a symlink into the
        # shipped root would have won the exemption after admission.
        if ap.admitted_builtin:
            return "proceed"
        if third_party_ceiling_closed(name) is None:
            return "proceed"
        # Audited through the gate itself, ONCE, at the point of acting: the poll
        # above deliberately writes no row (see third_party_ceiling_closed), so this
        # is what puts the revocation in the audit trail, with the gate's own reason.
        #
        # The audited answer is also the one ACTED on, and the re-ask is not
        # ceremony: the poll and this call are two separate reads, and an operator
        # can turn the flag back on between them. The gate then ADMITS the app, so
        # stopping it would revoke trust that was restored, while the audit row for
        # the stop would read "allowed". Deferring to this answer costs one extra
        # admission row in a window that is rarely entered.
        reason = app_execution_denied(
            name,
            action="health_watch_ceiling_revocation",
            caller="gateway",
        )
        if reason is None:
            return "proceed"
        logger.warning(
            "App %s: third-party execution is not permitted; stopping its "
            "backend on port %d — %s",
            name,
            ap.port,
            reason,
        )
        # Scrub the MCP registration FIRST, while `ap` is still the tracked record.
        # `stop_app_backend` does no MCP work at all, and returning below skips the
        # exited-backend branch that is the only other place an entry is reconciled
        # — so the app's url would stay in mcp.json pointing at a port nothing
        # serves, which breaks EVERY kiro session (connect failure, retries, hard
        # error) until the next boot reconcile. That is the same damage the boot MCP
        # reconcile in `start_enabled_app_backends` exists to repair. `_demote`
        # reaches the scrub through `_set_backend_health(healthy=False)`, under the
        # health serialization this thread already uses; the tri-state
        # `mcp_healthy` branch mirrors the exited-backend one, because a demote that
        # does not change `healthy` still has to unwind an entry that never landed.
        with _lock:
            was_healthy = ap.healthy
            mcp_state = ap.mcp_healthy
        if was_healthy:
            _demote(ap, reason=f"third-party execution revoked ({reason})")
        elif mcp_state is not False:
            _retry_mcp_reconcile(ap, healthy=False)
        # `_expected` so a record that was replaced between the sweep's identity
        # check and here is not stopped on its predecessor's evidence.
        # `_retry_if_serving` buys the stricter reading of an adopted backend whose
        # recorded PIDs fail to confirm: a port that still answers reports failure
        # with tracking intact rather than success, which is what this caller needs.
        stopped = stop_app_backend(name, _expected=ap, _retry_if_serving=health_path)
        with _lock:
            still_tracked = _processes.get(name) is ap
        if still_tracked:
            # `stop_app_backend` RESTORES tracking whenever it signalled NOTHING —
            # an adopted backend with no recorded PIDs, one whose recorded PIDs no
            # longer match their adoption identity (a supervisor replaced the
            # process), or a stop that raised. In every one of those the app is
            # very likely still serving, so exiting here would abandon the worst
            # case: un-trusted code answering its port, nothing retrying the
            # revocation, nothing watching its liveness. Keep sweeping instead, one
            # attempt per interval — the same shape the exited-backend branch below
            # uses — and let the denial row repeat, because "code the operator
            # un-trusted is still running" is a fact that stays true until it is not.
            #
            # Re-bind the owners so the retry has PIDs it can name. Without it every
            # retry re-reads the same stale identity token and can never signal, so
            # the loop would log forever without converging. The ``retry`` verdict is
            # what keeps the health judgement from running while this is true: a
            # still-serving port would otherwise be read as a recovery and promoted.
            rebound = ap.proc is None and _rebind_adopted_owners(ap, health_path)
            logger.warning(
                "App %s: could not stop a backend the ceiling does not admit; "
                "retrying next sweep (owners %s)",
                name,
                "re-bound" if rebound else "unchanged",
            )
            return "retry"
        if not stopped:
            logger.warning(
                "App %s: backend record was already gone when the closed ceiling "
                "was enforced; nothing left to stop",
                name,
            )
        # RETAINED cleanup. `_set_backend_health` advances `mcp_healthy` only on a
        # landed write, so a transient failure leaves it not-False while the demote
        # above still reported success — and the record is popped by now, so the
        # identity-gated reconcile can never land it. The exited-backend branch keeps
        # sweeping until the entry is confirmed gone; this path cannot, so it scrubs
        # by NAME instead, which needs no record. Recovery otherwise waits for the
        # next boot reconcile while a dead url breaks every kiro session.
        if ap.mcp_healthy is not False:
            try:
                # circular import: bridges imports from backend, so defer to call time.
                from kiro_crew.apps.bridges import _deregister_mcp_servers

                # The scrub is keyed on the app NAME, so it cannot tell this record's
                # stale entry from a SUCCESSOR's live one. A re-enable racing this
                # revocation can have started and registered a replacement already, and
                # removing its entry would leave a running backend with no reachable
                # tools. Held under the reconcile lock so the successor's registration
                # cannot land between the check and the scrub, and skipped when a
                # successor is TRACKED AND PAST ITS START: that record owns the
                # registration, and a successor admitted under a closed ceiling is the
                # sweep's next candidate anyway.
                #
                # A ``starting`` placeholder is NOT such a successor. It is installed
                # before the spawn to claim the name, so it owns no registration yet,
                # and a start that then fails removes it — leaving no record for any
                # later sweep to act on, and this record's dead url in `mcp.json` with
                # nothing left that would ever scrub it. Scrubbing past a placeholder is
                # safe in the other direction too: it has registered nothing to remove,
                # and a start that succeeds registers fresh afterwards, serialized
                # behind the same reconcile lock this holds.
                with _health_reconcile_lock:
                    with _lock:
                        successor = _processes.get(name)
                    if successor is not None and not successor.starting:
                        logger.info(
                            "App %s: leaving its MCP entry to the successor record that "
                            "now owns it",
                            name,
                        )
                        return "stopped"
                    removed = _deregister_mcp_servers(name)
            except Exception:  # noqa: BLE001 - reported, never swallowed
                logger.error(
                    "App %s: could not scrub its MCP entry after revoking execution; "
                    "a dead url may remain until the next gateway start",
                    name,
                    exc_info=True,
                )
            else:
                logger.warning(
                    "App %s: scrubbed %d MCP server entr(y/ies) by name after "
                    "revoking execution, because the reconcile did not land",
                    name,
                    removed,
                )
        return "stopped"
    except Exception:  # noqa: BLE001 - see the docstring; a dead watch is worse
        logger.warning(
            "App %s: could not act on a closed execution ceiling; retrying next sweep",
            name,
            exc_info=True,
        )
        return "proceed"


def _watch_backend_health_sweeps(ap: AppProcess, health_path: str) -> None:
    """Keep re-checking an already-healthy backend so ``healthy`` can go back to False.

    Without this the startup poll would leave ``healthy`` a write-once cache: a backend
    that died an hour later would still be routed to by the reverse proxy
    (:func:`get_app_backend_port` gates purely on the flag) and still reported
    ``healthy`` by ``/api/apps``.

    Liveness is checked cheapest-first. For a backend we spawned, ``Popen.poll()``
    answers from an already-reaped exit status with no syscall to the app at all and is
    DECISIVE — an exited process cannot come back on its own, so one observation demotes
    it and the watch stops. An adopted backend has no ``Popen`` handle (it belongs to
    another supervisor) and is judged by the health endpoint alone.

    An HTTP failure from a process that is still alive is NOT decisive: it may be a slow
    request or a GC pause, so demotion needs `_HEALTH_WATCH_FAILURES` consecutive misses
    and stays REVERSIBLE — the watch keeps running and re-promotes on the next success,
    which is what lets an app that wedged briefly heal without operator action.

    Exits when the record stops being the tracked one for its app: ``stop_app_backend``
    pops it and a restart replaces it, so this needs no separate teardown — the same
    "not the tracked record" guard the startup poll uses.
    """
    consecutive_failures = 0
    consecutive_healthy_sweeps = 0
    while True:
        time.sleep(_HEALTH_WATCH_INTERVAL)
        with _lock:
            # Identity, not name: a stop/start under the same name installs a NEW record
            # with its own watch, and demoting that one from here would take a backend
            # offline on evidence gathered about its predecessor.
            if _processes.get(ap.app_name) is not ap:
                return
            was_healthy = ap.healthy
            mcp_healthy = ap.mcp_healthy
            proc = ap.proc

        # Re-read the execution ceiling before judging health, because a backend the
        # operator does not admit must stop whether it is healthy or not.
        ceiling = _revoke_if_ceiling_closed(ap, health_path)
        if ceiling == "stopped":
            return
        if ceiling == "retry":
            # The ceiling is closed and the backend would not stop, so this sweep is
            # NOT allowed to judge health. The revocation demoted the record, which
            # makes `was_healthy` False from the next sweep on; the probe below then
            # sees the port an external supervisor keeps alive, reads
            # `healthy != was_healthy`, and PROMOTES — re-registering in mcp.json the
            # tools the revocation just scrubbed. The app would be dispatchable again
            # for half of every interval, flapping in and out while the operator
            # believes its trust is withdrawn. The stop keeps being retried; nothing
            # is re-promoted under a closed ceiling.
            continue

        if proc is not None and proc.poll() is not None:
            # A dead Popen never revives, so there is no health verdict left to reach —
            # but the watch may not leave until the MCP entry is actually out. This is
            # the one place where giving up strands the dead URL permanently: nothing
            # else revisits an exited backend, so a scrub that did not land would stay
            # unlanded and kiro-cli would keep dialing it every session. Keep sweeping
            # (one attempt per interval) until the entry is reconciled or the record is
            # dropped by stop_app_backend.
            #
            # `mcp_healthy` gates this as well as `healthy`: an entry that still says
            # healthy has to come out even when the flag was moved without the write
            # landing. It is TRI-STATE, and only `False` means "confirmed scrubbed" —
            # `None` is *unknown*, which is what a failed startup reconcile leaves
            # behind, and treating it as "nothing to unwind" would abandon exactly the
            # entry that most needs removing.
            if was_healthy:
                _demote(ap, reason=f"process exited (rc={proc.returncode})")
            elif mcp_healthy is not False:
                _retry_mcp_reconcile(ap, healthy=False)
            with _lock:
                dropped = _processes.get(ap.app_name) is not ap
                reconciled = ap.mcp_healthy is False
            if dropped:
                return
            if reconciled:
                _restart_exited_backend(ap, proc.returncode)
                return
            continue

        probed = _health_probe(ap.port, health_path)
        if probed.healthy:
            consecutive_failures = 0
            consecutive_healthy_sweeps += 1
            healthy = True
        else:
            consecutive_failures += 1
            consecutive_healthy_sweeps = 0
            healthy = was_healthy and consecutive_failures < _HEALTH_WATCH_FAILURES

        if healthy != was_healthy:
            if healthy:
                # An ADOPTED record carries the PIDs `stop_app_backend` will signal and
                # `uninstall` will act behind. Those were captured at adoption, and a
                # recovery means the EXTERNAL supervisor put something back — quite
                # possibly a different process. Promoting without re-binding ownership
                # would mark the record freshly-valid while its identities name a process
                # that is gone, so stop would signal the wrong PIDs (or none) and leave
                # the live replacement running while its files are mutated or removed.
                # Refuse the promotion instead: unhealthy-but-serving is recoverable on
                # the next sweep, a mis-bound owner set is not.
                if ap.proc is None and not _rebind_adopted_owners(ap, health_path):
                    continue
                _promote(ap)
            else:
                _demote(
                    ap,
                    reason=(
                        f"{consecutive_failures} consecutive failed health probes "
                        f"(last: {probed.detail}){_health_failure_hint(probed)}"
                    ),
                )
        elif mcp_healthy != healthy:
            # The verdict is unchanged but mcp.json never caught up — a previous
            # reconcile failed. Retry it here rather than waiting for the next health
            # transition, which for a backend that now stays put would never arrive.
            _retry_mcp_reconcile(ap, healthy=healthy)

        if consecutive_healthy_sweeps >= _RESTART_STABLE_SWEEPS:
            with _lock:
                recovered_attempts = (
                    _restart_attempts.pop(ap.app_name, 0)
                    if _processes.get(ap.app_name) is ap and ap.healthy
                    else 0
                )
            if recovered_attempts:
                logger.info("App %s backend recovered after restart", ap.app_name)


def _supervise_backend_health(ap: AppProcess, health_path: str) -> None:
    """Thread target: wait for the backend to come up, then watch it for as long as it
    stays tracked."""
    if _health_check_loop(ap, health_path) is not None:
        _watch_backend_health(ap, health_path)
        return
    # A process can stay unhealthy through the short startup poll and exit later.
    # Hand every still-tracked record to the ordinary watch so it can demote a live
    # unhealthy backend, observe a later exit, and enter the restart sequence.
    with _lock:
        still_tracked = _processes.get(ap.app_name) is ap
    if still_tracked:
        _watch_backend_health(ap, health_path)


def _start_health_supervisor(ap: AppProcess, health_path: str) -> None:
    """Run :func:`_supervise_backend_health` on this backend's own daemon thread.

    The record is handed over directly, so the supervisor is bound to this generation
    from the moment of the spawn — there is no window between inserting the record and
    the thread resolving it in which a restart could substitute a different one.
    """
    threading.Thread(
        target=_supervise_backend_health,
        args=(ap, health_path),
        daemon=True,
        name=f"app-health-{ap.app_name}",
    ).start()


def _start_adopted_health_watch(ap: AppProcess, health_path: str) -> None:
    """Watch an ADOPTED backend, which skipped the startup poll by already being healthy.

    Adoption proves the instance is serving right now, so there is nothing to wait for —
    but that made ``healthy`` write-once on this path too, and an external instance is
    exactly the kind we do not control the lifetime of. It has no ``Popen`` handle, so
    the watch judges it by its health endpoint alone.
    """
    threading.Thread(
        target=_watch_backend_health,
        args=(ap, health_path),
        daemon=True,
        name=f"app-health-{ap.app_name}",
    ).start()
