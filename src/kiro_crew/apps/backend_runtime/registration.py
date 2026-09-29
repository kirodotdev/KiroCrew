"""Health-gated MCP registration: the one transition that moves ``healthy`` and mcp.json.

Every health verdict reaches the app's MCP and agent state through
``_set_backend_health``, which re-checks the record's identity THROUGH the reconcile
under ``_health_reconcile_lock``: the MCP writers key on the app NAME, so a verdict about
a replaced record would otherwise scrub the successor's servers or republish a dead port.
``AppProcess.mcp_healthy`` records what last LANDED, so a reconcile that did not land is
retried rather than stranded. A promotion requires a positively enabled app; a demotion
never reads enablement.
"""

from __future__ import annotations

import logging

from kiro_crew.apps.backend_runtime import _FACADE
from kiro_crew.apps.backend_runtime.tracking import (
    AppProcess,
    _health_reconcile_lock,
    _lock,
    _processes,
)
from kiro_crew.apps.manager import app_enabled_state

logger = logging.getLogger(_FACADE)


def _gate_mcp_registration(app_name: str, port: int, *, healthy: bool) -> bool:
    """Register the app's MCP servers once its backend is healthy, or scrub them if not.

    Called from the health-check loop so the global mcp.json never carries an HTTP MCP url
    for an app whose backend isn't actually serving (registering with an optimistic
    pre-health port would leave a dead url for an enabled app whose backend never
    became healthy, breaking every kiro-cli session). On
    health success we (re)register with the confirmed live port; on failure we deregister
    so no dead entry survives. Never raises — registration must not crash the health loop.

    Returns whether the reconcile landed. The caller records that, because the health
    FLAG moves whether or not mcp.json could be written: without a success signal a
    transient write failure would strand a dead (or missing) entry until the next health
    transition, which for a backend that then stays put never comes."""
    try:
        if healthy:
            # circular import: bridges imports backend.get_app_backend_port, so deferring
            # this import to call time breaks the backend ↔ bridges module cycle.
            from kiro_crew.apps.bridges import reregister_app_mcp_servers

            # Symmetric with the scrub below: the agent JSONs carry the server spec, so a
            # registration whose agent half failed has not made the tools reachable — and
            # letting `mcp_healthy` advance on it would strand the app's agent without
            # its MCP tools, with nothing left to retry.
            register_io_failures: list[str] = []
            reregister_app_mcp_servers(app_name, live_port=port, io_failures=register_io_failures)
            if register_io_failures:
                logger.warning(
                    "App %s: %d agent(s) could not be rewritten after MCP registration "
                    "(%s); reporting the reconcile unlanded so the watch retries",
                    app_name,
                    len(register_io_failures),
                    ", ".join(register_io_failures),
                )
                return False
        else:
            # circular import: see above — bridges ↔ backend cycle, deferred to call time.
            from kiro_crew.apps.bridges import scrub_backend_mcp_url

            # NOT a blanket deregister. An app's stdio MCP servers are launched by
            # kiro-cli itself and have no port to be dead, so dropping them because an
            # HTTP backend died takes working tools away for a reason that has nothing to
            # do with them. scrub_backend_mcp_url pops the HTTP entry and keeps the rest
            # — falling back to removing everything when the manifest cannot say which is
            # which, since the dead url must not survive on the strength of not knowing.
            scrub_unreconciled: list[str] = []
            kept = scrub_backend_mcp_url(app_name, unreconciled=scrub_unreconciled)
            if scrub_unreconciled:
                logger.warning(
                    "App %s: the scrub could not be completed (%s); reporting it "
                    "unlanded so the watch retries",
                    app_name,
                    "; ".join(scrub_unreconciled),
                )
                return False
            if kept:
                logger.info(
                    "App %s: kept %d backend-independent MCP server(s) after the scrub",
                    app_name,
                    len(kept),
                )
            # The scrub is only half the removal: an app's materialized agent JSONs COPY
            # the server's launch spec, and the agent config is what kiro-cli loads. So
            # clearing the global map alone leaves the agents still naming the dead url —
            # the exact outage this gate exists to prevent, just one file over.
            # Registration already refreshes agents for this reason; mirror it here.
            #
            # A failed refresh makes the whole reconcile UNLANDED rather than being
            # swallowed. Registration treats its own refresh as non-fatal, but that path
            # has no retry behind it, so non-fatal there means "do not fail the
            # registration". Here the watch retries, an idempotent re-scrub is cheap, and
            # the alternative is a dead url left permanently in the file kiro-cli reads.
            from kiro_crew.apps.bridges import refresh_app_agents

            # refresh_app_agents, NOT a hand-rolled re-materialization: it already
            # carries the two guards this path must honour — a `resources="app"` app
            # registers its own agents and the gateway must not publish duplicates, and
            # a denied app's agents must be SCRUBBED rather than rewritten back into
            # dispatchable existence. Both return an empty list, which is "nothing for us
            # to do" rather than a failure; only the io_failures collector means retry.
            # The agent refresh RE-MATERIALIZES this app's agent configs. Unlike the
            # scrub above — always safe, and therefore never gated — that is a WRITE, and
            # for an app the operator has disabled it puts back the very files a
            # concurrent `deregister_app` just removed. Checked BEFORE and AFTER: the CLI
            # runs in another process, so neither check can be atomic with the write, and
            # only the pair converges.
            # Deleting happens only on a CONFIRMED disable. `_drop_disabled_app_resources`
            # unlinks materialized agents, taking user-owned fields with them, and
            # `installed.json` can fail to read transiently — so an UNKNOWN state must
            # not be collapsed into "disabled". It reports unlanded instead and the watch
            # retries. The cleanup's own result is the reconcile's result, because
            # deregister_app reports softly and discarding it would record a removal that
            # never happened.
            enabled = _app_enabled_state(app_name)
            if enabled is False:
                return _drop_disabled_app_resources(app_name)
            if enabled is None:
                return False  # unknown: neither refresh nor delete; try again next sweep
            scrub_io_failures: list[str] = []
            refresh_app_agents(app_name, io_failures=scrub_io_failures)
            enabled = _app_enabled_state(app_name)
            if enabled is False:
                return _drop_disabled_app_resources(app_name)
            if enabled is None:
                return False
            if scrub_io_failures:
                logger.warning(
                    "App %s: %d agent(s) could not be rewritten after the MCP scrub (%s); "
                    "reporting the reconcile unlanded so the watch retries",
                    app_name,
                    len(scrub_io_failures),
                    ", ".join(scrub_io_failures),
                )
                return False
        return True
    except Exception as exc:  # noqa: BLE001 — health loop must never crash on reconcile
        logger.warning("Health-gated MCP registration failed for app %s: %s", app_name, exc)
        return False


def _app_enabled_state(app_name: str) -> bool | None:
    """Tri-state enablement: True, False, or None when it could not be read.

    The distinction is load-bearing, because the two callers want OPPOSITE defaults on an
    unreadable state. Refusing to ADD resources when enablement is unknown is safe — the
    app stays as it is. DELETING them when it is unknown is not: `_drop_disabled_app_resources`
    unlinks materialized agents, taking the user-owned fields `_preserve_user_agent_edits`
    carries, and `installed.json` can fail to read transiently. Collapsing "unknown" into
    "disabled" would destroy data over a temporary fault.
    """
    try:
        # `app_enabled_state`, NOT `is_app_enabled`: the latter returns False for BOTH a
        # deliberate disable and an unreadable metadata file, because `_read_installed`
        # answers None to both. Trusting that collapsed False would make this whole
        # tri-state a no-op for the transient fault it exists to catch — the read error
        # never raises, so the `except` below would never see it.
        return app_enabled_state(app_name)
    except Exception as exc:  # noqa: BLE001 — unknown is a state, not a crash
        logger.warning(
            "App %s: could not read its enabled state: %s",
            app_name,
            exc,
        )
        return None


def _drop_disabled_app_resources(app_name: str) -> bool:
    """Remove everything registered for an app that turned out to be disabled.

    Returns whether the removal COMPLETED. ``deregister_app`` reports most problems
    softly, in ``RegistrationResult.errors`` rather than by raising, so a failed removal
    looks identical to a clean one unless that list is read. Idempotent with the CLI's
    own deregistration, so running it a second time costs nothing.
    """
    try:
        # circular import: bridges ↔ backend, deferred to call time.
        from kiro_crew.apps.bridges import deregister_app

        result = deregister_app(app_name)
    except Exception as exc:  # noqa: BLE001 — must not crash the watch
        logger.warning("App %s: could not drop a disabled app's resources: %s", app_name, exc)
        return False
    errors = list(getattr(result, "errors", None) or [])
    if errors:
        logger.warning(
            "App %s: dropping a disabled app's resources did not complete (%s)",
            app_name,
            "; ".join(errors),
        )
        return False
    return True


def _undo_promotion_of_disabled_app(ap: AppProcess) -> bool:
    """Remove what a promotion registered for an app disabled while it was being written.

    The enabled check and the write CANNOT be atomic: ``kirocrew app disable`` runs in
    another process, so there is no lock to share with it. Ordering closes the interleave
    where the flag is read after the resources come down; this closes the other one,
    where the check passes and the disable completes before the write lands. Verifying
    afterwards and undoing is the convergence that is actually available.

    ``deregister_app`` is idempotent and removes exactly what the promotion put back, so
    running it a second time after the CLI's own call costs nothing.

    Returns whether the removal COMPLETED. ``deregister_app`` reports most problems
    softly, in ``RegistrationResult.errors`` rather than by raising, so a failed removal
    looks identical to a clean one unless that list is read. ``mcp_healthy`` therefore
    only moves to False on a complete success — leaving it otherwise is what makes the
    next sweep try again, instead of recording a removal that did not happen and letting
    a disabled app stay dispatchable.
    """
    with _lock:
        if _processes.get(ap.app_name) is ap:
            ap.healthy = False
    if not _drop_disabled_app_resources(ap.app_name):
        logger.warning(
            "App %s: undoing the registration of a now-disabled app did not complete; "
            "retrying on the next sweep",
            ap.app_name,
        )
        return False
    with _lock:
        if _processes.get(ap.app_name) is ap:
            ap.mcp_healthy = False
    logger.warning(
        "App %s was disabled while its recovery was being registered; the registration "
        "has been undone",
        ap.app_name,
    )
    return True


def _set_backend_health(ap: AppProcess, *, healthy: bool) -> bool:
    """Flip ``ap.healthy`` and move its MCP entry, only while ``ap`` is still tracked.

    The identity re-check has to stay effective THROUGH the reconcile, not merely
    alongside the flag write, because the MCP writers key on the app NAME rather than on
    this record: `_deregister_mcp_servers` removes every ``<app>:`` entry, so a verdict
    formed about a record that has since been replaced would scrub the SUCCESSOR's live
    servers, and a stale re-register would publish the predecessor's dead port — the
    dead-URL outage the health gating exists to prevent.

    `_lock` cannot simply be held across the reconcile (it does manifest and config file
    I/O, and the proxy's get_app_backend_port must not block behind it), so the ordering
    is established with `_health_reconcile_lock` instead. That is sufficient because
    every health-driven reconcile takes it: passing the identity check proves the
    successor is not yet in `_processes`, hence has not registered, and its own
    registration must then queue behind this one — so the last write is always the
    live record's.

    Returns True if the transition was applied.
    """
    with _health_reconcile_lock:
        # IDENTITY FIRST. The undo below deregisters by app NAME, so running it for a
        # record that is not the tracked one would delete the SUCCESSOR's
        # resources — and an unreadable enabled state is exactly the case that would
        # send a retired watcher down that path.
        with _lock:
            if _processes.get(ap.app_name) is not ap:
                return False
        # Only a promotion is gated; a demotion's scrub must never be blocked — and must
        # not even READ the enabled state, which is a file access it has no use for.
        enabled = _app_enabled_state(ap.app_name) if healthy else True
        if enabled is not True:
            # Undo ONLY on a CONFIRMED disable. The undo deregisters, which unlinks
            # materialized agents and takes the user-owned fields with them, and
            # `installed.json` can fail to read transiently — an unknown state must not
            # be allowed to destroy data over a temporary fault. Unknown simply refuses
            # the promotion and tries again next sweep.
            #
            # When it IS confirmed disabled and the record still believes something of
            # ours is registered, an earlier undo did not land; this is where it is
            # retried, because nothing else revisits a disabled app.
            if enabled is False and ap.mcp_healthy is not False:
                _undo_promotion_of_disabled_app(ap)
            return False
        with _lock:
            if _processes.get(ap.app_name) is not ap:
                return False
            unchanged = ap.healthy == healthy
            ap.healthy = healthy
            # Short-circuit ONLY when the verdict did not change. `mcp_healthy` can be
            # stale in the other direction after a partial reconcile — an MCP write that
            # landed followed by an agent write that did not leaves it unmoved — so on a
            # TRANSITION the reconcile has to run even when the two happen to agree, or
            # a demotion would skip the scrub and leave the dead url registered.
            if unchanged and ap.mcp_healthy == healthy:
                return True  # already reconciled — nothing to rewrite
        if _gate_mcp_registration(ap.app_name, ap.port, healthy=healthy):
            with _lock:
                # Only a reconcile that LANDED updates the record, so a transient
                # failure leaves `mcp_healthy` out of step with `healthy` and the
                # watch's next sweep tries again. Re-check identity: the write
                # happened outside `_lock`.
                if _processes.get(ap.app_name) is ap:
                    ap.mcp_healthy = healthy
        # VERIFY, because the check above could not be atomic with the write: a disable
        # in the CLI's process can complete in between, and leaving the registration
        # would keep a disabled app dispatchable.
        # Same asymmetry on the verify: only a CONFIRMED disable undoes. An unknown
        # state here leaves the registration standing — the pre-check had confirmed the
        # app enabled, so the likely reading is a momentary read fault, and deleting on
        # that basis is the unrecoverable direction.
        if healthy and _app_enabled_state(ap.app_name) is False:
            _undo_promotion_of_disabled_app(ap)
            return False
        return True


def _demote(ap: AppProcess, *, reason: str) -> None:
    """Mark a backend unhealthy and scrub its MCP entry. Reversible — see _promote."""
    if _set_backend_health(ap, healthy=False):
        logger.warning(
            "App %s backend went unhealthy on port %d — %s",
            ap.app_name,
            ap.port,
            reason,
        )


def _promote(ap: AppProcess) -> None:
    """Mark a backend healthy again after a demotion and re-register its MCP servers."""
    if _set_backend_health(ap, healthy=True):
        logger.info("App %s backend recovered on port %d", ap.app_name, ap.port)


def _retry_mcp_reconcile(ap: AppProcess, *, healthy: bool) -> None:
    """Re-attempt a reconcile that did not land, without re-announcing its transition.

    The health verdict has not changed here — only mcp.json is behind — so this logs at
    debug rather than repeating the demote/recover line every sweep until it succeeds.
    """
    if _set_backend_health(ap, healthy=healthy):
        logger.debug(
            "App %s: retried MCP reconcile (healthy=%s) on port %d",
            ap.app_name,
            healthy,
            ap.port,
        )
