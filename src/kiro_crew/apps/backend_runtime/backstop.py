"""Level-triggered backstop that revives an enabled app whose backend record was lost.

The "an enabled app's backend never stays dead" invariant is carried in-process by one
edge-triggered chain: the per-record health watch (:mod:`supervision`) observes the exit
and hands the record to the restart loop (:mod:`restart`). That chain is the PRIMARY
mechanism and keeps the immediate first replacement and the fast ramp, and it is already
hardened against a single fault -- ``_watch_backend_health`` restarts its own sweep on
any exception, and a dead-but-tracked record is handed to the restart loop. But the chain
only acts while a RECORD is tracked. If ``_processes[name]`` is dropped WITHOUT a
successor -- a lifecycle path outside the declared owners, or a future refactor that pops
the record on an error path and forgets to restore it -- nothing holds the app any more,
and it is dead until the next gateway start (where boot's level-triggered pass picks it
up).

This pass re-seats exactly that abandoned case, periodically at a COARSE interval.

**It revives only an app the gateway has SUCCESSFULLY SEATED.** ``_seated_backends``
(``tracking``) holds every app name a live backend record was published for in this
process -- boot, enable, or a restart replacement. Membership is the gate that marks a
record the gateway itself once held and lost: a seated name already passed governance,
admission and execution vetting AT THE SEAT, and every deliberate keep-down path clears
it (``stop_app_backend`` discards it, and disable, uninstall, trust revoke, update,
gateway shutdown and the health watch's execution-ceiling revocation all route through
``stop_app_backend``; the one keep-down path that does NOT -- the restart loop's
non-transient activation-policy refusal, which has already popped the record and so has
no process to stop -- discards the name itself). So an app the operator or a policy took
down is not in the set and is never revived.

Seating is NOT, on its own, a permanent spawn licence. A record can be lost by a
lifecycle path outside the declared owners AFTER an administrator has tightened
POLICY ∩ PROFILE or admission -- the exact case this pass exists for -- so before it
spawns, ``_revive_one_backend`` re-vets activation with the SAME ``_activation_denied``
check the boot, enable and restart spawn paths run. A non-transient deny discards the
seated name (a deliberate keep-down, audited) so later sweeps do not re-attempt the
backend the current ceiling refuses; a transient evaluator error leaves the name and
retries next sweep. The vet runs only on an actual candidate (a lost record for an
enabled, seated app), not on every sweep, so it does not flood the audit trail the way a
per-sweep poll would. The backstop is not the governance enforcer -- it re-seats a record
the gateway once held and lost -- but it never spawns past the current activation ceiling.

Candidate rule (all read under one ``_lock`` snapshot where they touch shared state):

* enabled (per ``list_apps``, re-read fail-closed via ``_app_enabled_state`` -- a
  tri-state ``None`` is not a licence to start) and declares a backend entry point;
* NOT Dev Fleet (it needs the gateway's bound port at spawn, started only by the deferred
  boot wave);
* has NO tracked record (a STARTING placeholder, a live record, an adopted record, or a
  dead-but-still-tracked record awaiting the restart loop all mean a record is present and
  owned by an existing path);
* its last lifecycle transition is a START, not a STOP (``stop_app_backend`` leaves the
  phase at STOP while ``enabled`` can still read true for a window);
* and is in ``_seated_backends`` (the gateway seated it before and lost the record).

It only starts; the one stop it can issue is of the exact replacement IT spawned, by
identity, when a teardown raced the spawn. It is a reviver, not a reaper.

Bounded cost: it wakes at :data:`_BACKSTOP_INTERVAL` (far coarser than the health watch),
does a cheap in-memory membership read, and attempts each eligible app at most once per
interval. A failed revive is never terminal -- the retry is bounded by the interval, not
abandoned -- so a transient fault self-heals on a later sweep while a persistently-broken
app costs only one attempt per interval (and, after the first, only a debug log line).
Everything is gated on the process-wide gateway shutdown signal so it never blocks a
clean exit.

Known limitation: ``_restart_exited_backend`` pops the record then
drains the dead root's tree with no lock held, so a sweep tick landing inside that drain
could adopt the child being drained. The window is a property of restart.py's teardown
ordering that predates this pass; the backstop (a coarse 300 s sweep) only adds one more
observer of it, and closing it at its source is tracked separately.
"""

from __future__ import annotations

import logging
import threading

from kiro_crew.apps.backend_runtime import _FACADE, _facade
from kiro_crew.apps.backend_runtime.registration import _app_enabled_state
from kiro_crew.apps.backend_runtime.restart import _activation_denied, shutdown_event
from kiro_crew.apps.backend_runtime.startup import DEV_FLEET_APP_NAME
from kiro_crew.apps.backend_runtime.termination import stop_app_backend
from kiro_crew.apps.backend_runtime.tracking import (
    _LIFECYCLE_START,
    _LIFECYCLE_STOP,
    AppProcess,
    _health_reconcile_lock,
    _lifecycle_generation,
    _lock,
    _processes,
    _seated_backends,
)
from kiro_crew.apps.manager import list_apps
from kiro_crew.sel import sel

logger = logging.getLogger(_FACADE)


# How often the backstop sweeps. This is a coarse SAFETY NET for the rare case that the
# edge-triggered restart chain was lost, not a supervisor; the health watch
# (``_HEALTH_WATCH_INTERVAL`` = 15s) plus the restart ramp own the common case and react
# in seconds. Five minutes bounds how long a lost record can strand an app without adding
# a steady poll any tighter than the restart loop's own steady cadence
# (``_RESTART_STEADY_INTERVAL`` = 300s), which is the floor below which re-starting a
# persistently-dead app buys nothing.
_BACKSTOP_INTERVAL = 300.0
# A failed revive is NEVER terminal: the backstop is a safety net, so an app that could
# not spawn this sweep is re-attempted on the NEXT one, indefinitely while it stays an
# eligible candidate. Giving up permanently would defeat the whole point -- a transient
# fault (process-limit exhaustion, a host briefly out of memory) would strand the app
# until the next gateway restart, which is exactly the failure mode this exists to avoid.
# The per-app counter therefore only THROTTLES THE LOG: the first failure warns, and
# further consecutive failures drop to debug so a persistently-broken app does not spam a
# warning every 300 s. The retry itself is bounded by ``_BACKSTOP_INTERVAL`` (one attempt
# per interval), the same steady cadence the restart loop falls back to.
_BACKSTOP_WARN_AFTER = 1


def _backstop_candidates() -> list[str]:
    """Enabled, seated apps that declare a backend entry point and have NO tracked record.

    See the module docstring for the full rule. Candidate selection is a cheap membership
    read -- enabled, seated, declares a backend, no tracked record, not mid-STOP -- so the
    quiet common case costs no governance call. Membership in ``_seated_backends`` marks a
    record the gateway itself once held and lost (every keep-down path clears the name,
    through ``stop_app_backend`` or the restart loop's direct discard on an activation
    refusal); it is NOT a spawn licence on its own. The activation/admission re-vet that
    guards the actual spawn runs per candidate in ``_revive_one_backend``, not here. The
    record, phase and seated checks are read under ONE ``_lock`` so they are a consistent
    snapshot.
    """
    candidates: list[str] = []
    for app_info in list_apps():
        if not app_info.get("enabled"):
            continue
        name = app_info.get("name", "")
        if not name or name == DEV_FLEET_APP_NAME:
            continue
        manifest = app_info.get("manifest", {})
        if not manifest.get("backend", {}).get("entryPoint"):
            continue
        with _lock:
            # A present record (live, adopted, STARTING, or dead-awaiting-restart) is
            # owned by an existing lifecycle path; a STOP phase is a deliberate teardown
            # in flight; and a name not in the seated set was never successfully started
            # by this gateway, so it is boot's/restart's concern, not a lost record.
            if _processes.get(name) is not None:
                continue
            if _lifecycle_generation.get(name, (0, _LIFECYCLE_START))[1] == _LIFECYCLE_STOP:
                continue
            if name not in _seated_backends:
                continue
        # Re-read enablement fail-closed: ``list_apps`` is a snapshot, and only a
        # positively-True reading licenses a start. This reads the app's OWN declared
        # state, not a governance/admission/execution policy (those are not re-run here).
        if _app_enabled_state(name) is not True:
            continue
        candidates.append(name)
    return candidates


def _revive_one_backend(name: str) -> AppProcess | None:
    """Seat a backend for one abandoned app, serialized against a concurrent stop.

    The candidate list is a snapshot: between building it and reaching here, a disable,
    trust revoke, update or uninstall can run ``stop_app_backend``, which pops the record,
    advances the generation to ``STOP`` and clears the seated name. The pre-check is
    re-made here under the locks and a generation SNAPSHOT is taken; the spawn then runs
    OUTSIDE the lock (it can take up to ~120 s -- an ``npm install`` plus the survival
    poll -- and ``_health_reconcile_lock`` is the single process-wide lock every stop,
    start and health write takes, so holding it across the spawn would stall the whole app
    subsystem).

    The spawn goes through the INTERNAL ``_start_app_backend`` (no lifecycle START
    transition -- the public entry would bump the generation to START and override a
    racing STOP), exactly as the health-watch restart loop does.

    After the spawn, the generation is re-read. The replacement is stopped (by its exact
    identity) and ``None`` returned only on a positively-decisive teardown: a STOP that
    raced the spawn, a gateway shutdown, or enablement read as positively ``False``. An
    unknown enablement read (tri-state ``None``, a transient ``installed.json`` fault) is
    deliberately NOT decisive -- stopping on it would record a STOP (and clear the seated
    name) and strand the still-enabled app on every later sweep -- so the replacement is
    left running and the ordinary health watch / next sweep handle it.

    Returns the seated record, or ``None`` when the pre-check found the app ineligible,
    the spawn produced nothing, or a decisive teardown raced the spawn.
    """
    with _health_reconcile_lock:
        with _lock:
            if _processes.get(name) is not None:
                return None
            snapshot = _lifecycle_generation.get(name, (0, _LIFECYCLE_START))
            if snapshot[1] == _LIFECYCLE_STOP:
                return None
            if name not in _seated_backends:
                return None
        # Enablement is re-read HERE (not only when the candidate list was built) because
        # a cross-process CLI disable writes disabled metadata without touching the
        # lifecycle generation, so the STOP-phase check alone would miss it. ``True`` only.
        if _app_enabled_state(name) is not True:
            return None

        # Re-vet activation before spawning. ``_seated_backends`` membership proves the
        # app passed governance/admission AT THE SEAT, but the record can be lost by a
        # lifecycle path outside the declared owners AFTER an administrator tightened
        # POLICY ∩ PROFILE or admission -- the exact "outside the declared owners" case
        # this pass exists for. Reviving on the seated mark alone would then respawn a
        # backend the CURRENT ceiling refuses. So the backstop runs the same vet the boot,
        # enable and restart spawn paths run, before it spawns. The vet runs only on an
        # actual candidate (a lost record for an enabled, seated app), not every sweep, so
        # it does not flood the audit trail the way a per-sweep poll would.
        verdict = _activation_denied(name, "backstop")
        if verdict.denied and not verdict.transient:
            logger.warning(
                "Backstop: not reviving app %s: blocked by activation policy: %s",
                name,
                verdict.denied,
            )
            try:
                sel().log_api_access(
                    caller="gateway",
                    operation="app_backend_backstop_revive",
                    outcome="denied",
                    resources=name,
                    error=verdict.denied,
                )
            except Exception as exc:  # noqa: BLE001 — denial remains fail-closed
                logger.debug("SEL audit failed for app %s backstop deny: %s", name, exc)
            # A non-transient refusal is a deliberate keep-down: discard the seated name so
            # later sweeps do not re-attempt the backend the current ceiling refuses, the
            # same way the restart loop's activation-refusal branch clears it.
            with _lock:
                _seated_backends.discard(name)
            return None
        if verdict.transient:
            # A governance-evaluator error (not an affirmative deny) is not decisive:
            # leave the seated name in place and retry on the next sweep, so a transient
            # evaluator fault does not strand a legitimately-enabled app.
            logger.warning(
                "Backstop: activation re-vet for app %s failed transiently; "
                "skipping this sweep and retrying next: %s",
                name,
                verdict.denied or "activation evaluation error",
            )
            return None

    # Spawn OUTSIDE both locks (see the docstring): the internal entry records no START
    # transition, so it cannot override a STOP a racing teardown sets while we are here.
    replacement = _facade()._start_app_backend(name)
    if replacement is None:
        return None

    with _health_reconcile_lock:
        with _lock:
            now = _lifecycle_generation.get(name, (0, _LIFECYCLE_START))
        raced_stop = now != snapshot and now[1] == _LIFECYCLE_STOP
        # Decisive teardown only: a raced STOP, a gateway shutdown, or enablement
        # positively False. An unknown (None) read is NOT decisive -- stopping on it would
        # record a STOP that strands the still-enabled app -- so the replacement is left
        # running for the health watch / next sweep.
        if raced_stop or shutdown_event.is_set() or _app_enabled_state(name) is False:
            logger.info(
                "Backstop: revive of app %s cancelled after spawn (teardown raced); "
                "stopping the replacement",
                name,
            )
            stop_app_backend(name, _expected=replacement)
            return None
    return replacement


def _run_backstop_sweep(failures: dict[str, int]) -> list[str]:
    """Revive every enabled, seated app that has lost its backend record.

    ``failures`` is the caller-owned per-app CONSECUTIVE-failure counter, mutated in
    place. It does NOT gate whether an app is attempted -- the backstop is a safety net
    and never gives up permanently -- it only throttles the log (warn the first time,
    debug thereafter) so a persistently-broken app does not warn every interval. Returns
    the app names a record was actually seated this sweep (empty on the common quiet case).

    Fail-isolated per app: one app's start raising or returning None must never skip the
    rest or crash the sweep thread.
    """
    seated: list[str] = []
    candidates = _backstop_candidates()
    # Drop counters for apps outside the candidate set (recovered, disabled, uninstalled,
    # or dropped from the seated set) so a later relapse starts from a clean log-throttle
    # state.
    for gone in [name for name in failures if name not in candidates]:
        failures.pop(gone, None)

    for name in candidates:
        if shutdown_event.is_set():
            break
        try:
            ap = _revive_one_backend(name)
        except Exception as exc:  # noqa: BLE001 — one app's failure must not skip the rest
            _record_backstop_failure(failures, name, str(exc))
            continue
        if ap is not None:
            failures.pop(name, None)
            seated.append(name)
            logger.warning(
                "Backstop: re-seated a lost-watch backend for enabled app %s on port %d "
                "(the edge-triggered restart chain had dropped its record)",
                name,
                ap.port,
            )
        else:
            _record_backstop_failure(failures, name, "spawn produced no record")
    return seated


def _record_backstop_failure(failures: dict[str, int], name: str, detail: str) -> None:
    """Count one failed revive and log it, WITHOUT ever skipping a future attempt.

    The first failure warns (it may be the lost-watch case the operator wants to know
    about); further consecutive failures drop to debug so a hard-down app does not warn
    every ``_BACKSTOP_INTERVAL``. The app is still retried next sweep regardless -- a
    transient fault (process-limit exhaustion, a host briefly out of memory) must
    self-heal, which a terminal cap would prevent.
    """
    failures[name] = failures.get(name, 0) + 1
    log = logger.warning if failures[name] <= _BACKSTOP_WARN_AFTER else logger.debug
    log(
        "Backstop: could not revive lost-watch backend for app %s "
        "(consecutive failure %d; will retry next sweep): %s",
        name,
        failures[name],
        detail,
    )


def _backstop_loop() -> None:
    """Daemon thread: sweep for lost-record backends every :data:`_BACKSTOP_INTERVAL`.

    Sleeps first so boot's own level-triggered pass (``start_enabled_app_backends``) has
    already run -- the backstop adds nothing in the first interval after boot. Exits
    promptly on the process-wide shutdown signal.
    """
    failures: dict[str, int] = {}
    while not shutdown_event.wait(_BACKSTOP_INTERVAL):
        try:
            _run_backstop_sweep(failures)
        except Exception:  # noqa: BLE001 — the backstop must never crash the gateway
            logger.warning("Backstop sweep failed; will retry next interval", exc_info=True)


_backstop_thread: threading.Thread | None = None
_backstop_thread_lock = threading.Lock()


def start_app_backend_backstop() -> None:
    """Launch the backstop daemon once. Idempotent; a second call is a no-op.

    Started by the gateway after the boot pass, so the first sweep is one interval after
    the gateway is up. Daemon so it never blocks shutdown; the loop also watches the
    shutdown event so it stops promptly rather than only when the process exits.
    """
    global _backstop_thread
    with _backstop_thread_lock:
        if _backstop_thread is not None and _backstop_thread.is_alive():
            return
        _backstop_thread = threading.Thread(
            target=_backstop_loop,
            daemon=True,
            name="app-backend-backstop",
        )
        _backstop_thread.start()
    logger.info(
        "App-backend backstop armed: sweeping for lost-record enabled-app backends " "every %.0fs",
        _BACKSTOP_INTERVAL,
    )
