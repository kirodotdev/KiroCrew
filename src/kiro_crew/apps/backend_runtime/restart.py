"""Restarting an exited spawned backend through the ordinary spawn path.

The health watch hands a still-tracked, exited record here once its MCP scrub landed.
The replacement is spawned by the facade's ``_start_app_backend``, so it gets the
pidfile record, health gate, MCP promotion and single-flight of every other start
without recording an external START; the lifecycle generation snapshot then tells a
later deliberate stop from a later start. Fast retries give way to a slow steady cadence
while the app stays positively enabled.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Literal

from kiro_crew import shutdown_event as _gateway_shutdown_event
from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.backend_runtime import _FACADE, _facade
from kiro_crew.apps.backend_runtime.pidfile import _forget_exited_leader_row
from kiro_crew.apps.backend_runtime.ports import _allocated_ports
from kiro_crew.apps.backend_runtime.registration import _app_enabled_state
from kiro_crew.apps.backend_runtime.termination import _drain_exited_root_tree, stop_app_backend
from kiro_crew.apps.backend_runtime.tracking import (
    _LIFECYCLE_START,
    _LIFECYCLE_STOP,
    AppProcess,
    _await_inflight_spawn,
    _health_reconcile_lock,
    _lifecycle_generation,
    _lock,
    _processes,
    _restart_attempts,
)
from kiro_crew.apps.manager import _app_activation_denied, _read_installed, get_app_manifest
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance_profiles import GOVERNANCE_ERROR_REASON
from kiro_crew.sel import sel

logger = logging.getLogger(_FACADE)


class _BackendShutdownEvent:
    """Expose timeout-aware waits for synchronous backend supervisor threads.

    The process-wide shutdown signal is asyncio-native and cannot be awaited from these
    threads. Polling it through a private never-set threading event preserves prompt
    shutdown without changing the shared async API.
    """

    _POLL_INTERVAL = 0.1

    def is_set(self) -> bool:
        return _gateway_shutdown_event.is_set()

    def wait(self, timeout: float) -> bool:
        if self.is_set():
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        sleeper = threading.Event()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self.is_set()
            sleeper.wait(min(remaining, self._POLL_INTERVAL))
            if self.is_set():
                return True


shutdown_event = _BackendShutdownEvent()
# A spawned backend that exits cannot recover by itself. The health watch first uses a
# bounded fast exponential ramp while the app is positively enabled. The first
# replacement attempt is immediate; seven subsequent waits cover a transient per-user
# service-manager outage of roughly 45 seconds with margin
# (0+1+2+4+8+16+30+30 = 91 seconds). After that ramp, retries continue indefinitely at
# a slow steady interval: an enabled app must not remain dead waiting for an operator,
# while the interval keeps persistent failures cheap. The counter resets only after the
# replacement remains healthy for `_RESTART_STABLE_SWEEPS`.
_RESTART_ON_EXIT_INITIAL_DELAY = 1.0
_RESTART_ON_EXIT_MAX_DELAY = 30.0
_RESTART_ON_EXIT_FAST_ATTEMPTS = 8
_RESTART_STEADY_INTERVAL = 300.0
_SETTLE_UNRESOLVED_WARN_AFTER = 20


@dataclass(frozen=True)
class ActivationVerdict:
    """Result of re-vetting an app before a restart attempt.

    ``denied`` carries an affirmative policy denial or evaluation error.
    ``transient`` identifies governance-evaluator errors so restart supervision
    refuses the current attempt while preserving its retry cadence.
    """

    denied: str | None = None
    transient: bool = False


def _activation_denied(app_name: str, action: str) -> ActivationVerdict:
    """Re-vet restart activation; only governance-evaluator errors are transient."""
    try:
        denied = _app_activation_denied(app_name, fail_closed=True)
    except PlatformCompositionError as exc:
        return ActivationVerdict(denied=f"platform composition error: {exc}")
    except Exception as exc:  # noqa: BLE001 — unexpected governance errors deny restart
        return ActivationVerdict(denied=f"governance re-vet error: {exc}")
    if denied:
        return ActivationVerdict(
            denied=denied,
            transient=denied.startswith(GOVERNANCE_ERROR_REASON)
            or denied.startswith("governance evaluation error:"),
        )
    installed = _read_installed(app_name)
    if installed is None or installed.origin != "builtin":
        try:
            admission_denied = app_admission_denied(
                app_name,
                manifest=get_app_manifest(app_name),
                action=action,
            )
        except Exception as exc:  # noqa: BLE001 — admission uncertainty is a hard denial
            return ActivationVerdict(denied=f"admission re-vet error: {exc}")
        if admission_denied:
            return ActivationVerdict(denied=admission_denied)
    return ActivationVerdict()


def _settle_superseding_start(
    app_name: str,
    ap: AppProcess,
    replacement: AppProcess | None,
) -> Literal["continue", "return_true", "return_false"]:
    """Resolve a process-table handoff without abandoning restart supervision.

    The caller holds neither ``_health_reconcile_lock`` nor ``_lock``. A STARTING
    record is intent, not a successor, so wait until it settles. Only a tracked
    non-STARTING successor, STOP/disable/shutdown, or restoration of ``ap`` is
    decisive; failed spawn cleanup owns port reservations.
    """
    unresolved_awaits = 0
    warned_unresolved = False
    while True:
        _await_inflight_spawn(app_name)
        with _health_reconcile_lock:
            with _lock:
                lifecycle_now = _lifecycle_generation.get(app_name, (0, _LIFECYCLE_START))
            if (
                lifecycle_now[1] == _LIFECYCLE_STOP
                or shutdown_event.is_set()
                or _app_enabled_state(app_name) is not True
            ):
                if replacement is not None:
                    stop_app_backend(app_name, _expected=replacement)
                return "return_false"
            with _lock:
                current = _processes.get(app_name)
                if current is ap:
                    return "continue"
                if current is None:
                    _processes[app_name] = ap
                    return "continue"
                if not current.starting:
                    return "return_true"
        # A timed-out await can leave a genuinely slow STARTING owner in place.
        # Keep waiting rather than treating unresolved intent as a successor.
        unresolved_awaits += 1
        if not warned_unresolved and unresolved_awaits >= _SETTLE_UNRESOLVED_WARN_AFTER:
            logger.warning(
                "App %s: superseding start unresolved after %d awaits; "
                "STARTING placeholder is not settling",
                app_name,
                unresolved_awaits,
            )
            warned_unresolved = True


def _restart_exited_backend(ap: AppProcess, returncode: int | None) -> bool:
    """Replace one exited, still-tracked backend through the ordinary spawn path.

    The caller reaches this only after the dead generation's MCP scrub landed. Keeping
    the dead record through the backoff lets ``stop_app_backend`` win naturally: its
    identity pop makes the final check fail before this function removes the record and
    starts anything. The replacement uses the normal spawn implementation (pidfile,
    health gate, MCP promotion, and single-flight) without recording an external START.
    A lifecycle generation snapshot then distinguishes a later STOP from a later START.
    Fast retries transition to a slow steady cadence rather than giving up while the app
    remains enabled.

    Exit invariant: this loop returns only after it published its own replacement, a
    tracked non-STARTING successor has assumed supervision, or STOP, disable, or
    shutdown made restart supervision unnecessary. A START generation records intent,
    not success, so its in-flight spawn is awaited with neither
    ``_health_reconcile_lock`` nor ``_lock`` held before that intent is decisive.
    """
    app_name = ap.app_name
    activation_error_warned = False
    steady_state_warned = False
    # The exited leader's row is dropped once, in the ``finally`` below, and never
    # inside the loop: every retry asks attribution again. The row carries the spawn
    # tree's instance token, and that token is what admits a pre-fork worker or a
    # detached child of this app still holding the port, so a removal taken while the
    # loop is still retrying leaves each later attempt unable to attribute the very
    # process it is trying to replace, and the app stays down for as long as its own
    # orphan keeps the port.
    #
    # The drop is GATED on ``leader_row_superseded``: it fires only once the loop has
    # committed to superseding this leader (the post-wait re-check popped it from the
    # tracking table, right before the tree drain), which is exactly where the base
    # removed the row inline. Every EARLY return -- a transient-unreadable
    # ``installed.json``, a shutdown, an identity that moved, a stop that won -- leaves
    # the row in place as the base did, so a leader that exited leaving workers on the
    # port keeps its handle: the reap can still census it, and adoption can still
    # attribute it, rather than the group becoming an unnamed orphan holding the port.
    leader_row_superseded = False
    try:
        while True:
            entered_steady_state = False
            identity_moved = False
            with _health_reconcile_lock:
                with _lock:
                    proc = ap.proc
                    if _processes.get(app_name) is not ap:
                        identity_moved = True
                    elif proc is None or proc.poll() is None:
                        return False
                    else:
                        previous_attempts = _restart_attempts.get(app_name, 0)
                if not identity_moved:
                    # ``None`` is deliberately fail-closed: an unreadable installed.json
                    # must not bring an app back after the operator may have disabled it.
                    if shutdown_event.is_set() or _app_enabled_state(app_name) is not True:
                        return False
                    attempt_number = previous_attempts + 1
                    steady_state = previous_attempts >= _RESTART_ON_EXIT_FAST_ATTEMPTS
                    if steady_state:
                        delay = _RESTART_STEADY_INTERVAL
                        with _lock:
                            if _processes.get(app_name) is not ap:
                                identity_moved = True
                            else:
                                entered_steady_state = not steady_state_warned
                    else:
                        delay = (
                            0.0
                            if previous_attempts == 0
                            else min(
                                _RESTART_ON_EXIT_INITIAL_DELAY * (2 ** (previous_attempts - 1)),
                                _RESTART_ON_EXIT_MAX_DELAY,
                            )
                        )
                    if not identity_moved:
                        if steady_state:
                            logger.info(
                                "App %s backend exited (rc=%s); restarting "
                                "(attempt %d, steady) in %.1fs",
                                app_name,
                                returncode,
                                attempt_number,
                                delay,
                            )
                        else:
                            logger.info(
                                "App %s backend exited (rc=%s); restarting "
                                "(fast attempt %d/%d) in %.1fs",
                                app_name,
                                returncode,
                                attempt_number,
                                _RESTART_ON_EXIT_FAST_ATTEMPTS,
                                delay,
                            )

            if identity_moved:
                # The tracked process is not this leader -- a successor took over, or a
                # stop/disable/shutdown ended it. Either way THIS loop is the losing
                # party, so it must NOT drop the row: the winning lifecycle operation
                # owns row cleanup. A successor recorded its own identity; a stop
                # decided whether to keep the row (an ordinary stop that tolerated a
                # surviving port-holder deliberately RETAINS it so a later re-enable can
                # re-attribute the survivor). Arming the drop here would delete that
                # retained row and strand the survivor unmanageable. So the row is left
                # in place on every identity-moved verdict, matching the base.
                settlement = _settle_superseding_start(app_name, ap, None)
                if settlement == "continue":
                    # ``ap`` is restored to tracking here, so it counts as live and
                    # unsuperseded; keep its row.
                    leader_row_superseded = False
                    continue
                return settlement == "return_true"

            if entered_steady_state:
                logger.warning(
                    "App %s backend still failing after %d fast restart attempts; "
                    "retrying every %.0fs while the app stays enabled (disable the app to stop)",
                    app_name,
                    _RESTART_ON_EXIT_FAST_ATTEMPTS,
                    _RESTART_STEADY_INTERVAL,
                )
                steady_state_warned = True

            if shutdown_event.wait(delay):
                return False

            # Re-check after waiting. ``stop_app_backend`` takes the same serialization
            # before popping, so a deliberate stop cannot race this removal into a respawn.
            post_wait_identity_moved = False
            with _health_reconcile_lock:
                if shutdown_event.is_set() or _app_enabled_state(app_name) is not True:
                    return False
                with _lock:
                    proc = ap.proc
                    if _processes.get(app_name) is not ap:
                        post_wait_identity_moved = True
                    elif proc is None or proc.poll() is None:
                        return False
                    else:
                        _processes.pop(app_name, None)
                        _allocated_ports.pop(app_name, None)
                        _restart_attempts[app_name] = previous_attempts + 1
                        lifecycle_snapshot = _lifecycle_generation.get(
                            app_name, (0, _LIFECYCLE_START)
                        )
                        # The loop has now committed to superseding this leader: it is
                        # popped from tracking and a replacement follows. This is where
                        # the base dropped the row inline, so it is where the deferred
                        # drop is armed -- every path that returns BEFORE reaching here
                        # keeps the row, matching the base.
                        leader_row_superseded = True

            if post_wait_identity_moved:
                # Same as the pre-wait identity-moved verdict above: THIS loop lost the
                # slot to a successor or a stop, so it does not own the row and must not
                # drop it -- the winning operation does. Leave the row in place.
                settlement = _settle_superseding_start(app_name, ap, None)
                if settlement == "continue":
                    # ``ap`` is restored to tracking here, so it counts as live and
                    # unsuperseded; keep its row.
                    leader_row_superseded = False
                    continue
                return settlement == "return_true"

            # The dead root's tree goes before its replacement is spawned: a launcher
            # whose forked server outlived it would otherwise keep the app's files (and
            # possibly its port) while a second server comes up beside it. Same drain
            # as the stop path; a root that took its whole tree with it costs one probe.
            if proc is not None and (
                _drain_exited_root_tree(app_name, proc, ap.pid_start_time, ap.spawn_instance)
                is False
            ):
                logger.warning(
                    "App %s: a descendant of the exited backend root (pid %d) survived the "
                    "drain; the replacement is spawned beside it",
                    app_name,
                    proc.pid,
                )
            if ap.log_fh:
                try:
                    ap.log_fh.close()
                except OSError:
                    pass

            verdict = _activation_denied(app_name, "restart")
            if verdict.denied and not verdict.transient:
                logger.warning(
                    "App %s backend not restarted: blocked by activation policy: %s",
                    app_name,
                    verdict.denied,
                )
                try:
                    sel().log_api_access(
                        caller="gateway",
                        operation="app_backend_restart",
                        outcome="denied",
                        resources=app_name,
                        error=verdict.denied,
                    )
                except Exception as exc:  # noqa: BLE001 — denial remains fail-closed
                    logger.debug("SEL audit failed for app %s restart deny: %s", app_name, exc)
                return False

            backend_still_declared = True
            if verdict.transient:
                evaluation_error = verdict.denied or "activation evaluation error"
                if not activation_error_warned:
                    logger.warning(
                        "App %s backend restart activation evaluation failed; "
                        "refusing this attempt and retrying on the restart cadence: %s",
                        app_name,
                        evaluation_error,
                    )
                    activation_error_warned = True
                try:
                    sel().log_api_access(
                        caller="gateway",
                        operation="app_backend_restart",
                        outcome="error",
                        resources=app_name,
                        error=evaluation_error,
                    )
                except Exception as exc:  # noqa: BLE001 — evaluation remains fail-closed
                    logger.debug("SEL audit failed for app %s restart error: %s", app_name, exc)
                replacement = None
            else:
                # The permit is a gateway-initiated exercise of the app's execution
                # grant with no operator in the loop, so SEL records it as it does the
                # denial: an operator reconstructing a trust timeline must see the
                # decision, not infer it from the spawn that followed.
                try:
                    sel().log_api_access(
                        caller="gateway",
                        operation="app_backend_restart",
                        outcome="allowed",
                        resources=f"{app_name} attempt={attempt_number}",
                    )
                except Exception as exc:  # noqa: BLE001 — the permit stands without its audit line
                    logger.debug("SEL audit failed for app %s restart allow: %s", app_name, exc)
                try:
                    replacement = _facade()._start_app_backend(app_name)
                    if replacement is None:
                        manifest = get_app_manifest(app_name)
                        backend_still_declared = bool(
                            manifest is not None and manifest.backend.entryPoint
                        )
                except Exception as exc:  # noqa: BLE001 — a raised spawn is a retryable failure
                    logger.warning(
                        "App %s backend restart attempt %d failed to spawn: %s",
                        app_name,
                        attempt_number,
                        exc,
                    )
                    replacement = None

            # The compare and any teardown are serialized with public START generation
            # bumps. A later START is intent only: the shared handoff below waits outside
            # both locks before deciding whether another supervisor really took over.
            with _health_reconcile_lock:
                with _lock:
                    lifecycle_now = _lifecycle_generation.get(app_name, (0, _LIFECYCLE_START))
                superseding_start = (
                    lifecycle_now != lifecycle_snapshot and lifecycle_now[1] == _LIFECYCLE_START
                )
                if lifecycle_now != lifecycle_snapshot and not superseding_start:
                    logger.info(
                        "App %s backend restart was cancelled after spawn; stopping replacement",
                        app_name,
                    )
                    if replacement is not None:
                        stop_app_backend(app_name, _expected=replacement)
                    return False
                if not superseding_start:
                    if shutdown_event.is_set() or _app_enabled_state(app_name) is not True:
                        logger.info(
                            "App %s backend restart was cancelled after spawn; stopping replacement",
                            app_name,
                        )
                        if replacement is not None:
                            stop_app_backend(app_name, _expected=replacement)
                        return False
                    if not backend_still_declared:
                        return True
                    if replacement is not None:
                        return True

            settlement = _settle_superseding_start(app_name, ap, replacement)
            if settlement == "continue":
                # ``_settle_superseding_start`` restores ``ap`` to the tracking table
                # here (nothing else was tracked), so this leader counts as live and
                # unsuperseded and its row must NOT be dropped by the ``finally``.
                # Disarm the flag; a later iteration re-arms it only if it commits to
                # superseding again.
                leader_row_superseded = False
                continue
            return settlement == "return_true"
    finally:
        if leader_row_superseded:
            _forget_exited_leader_row(app_name, ap)
