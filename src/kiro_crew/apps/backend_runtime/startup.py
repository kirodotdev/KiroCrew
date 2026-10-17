"""Gateway startup: reap, reconcile, vet, then spawn every enabled app's backend.

``start_enabled_app_backends`` reaps a prior generation's orphans, scrubs the MCP
entries of disabled apps, revokes the executable resources of apps no activation
boundary admits any more, vets the rest fail-closed, and spawns the admitted set
concurrently with their fixed ports reserved first. The Dev Fleet backend is vetted
with the rest but spawned by ``start_deferred_app_backends`` once the gateway's bound
port exists.
"""

from __future__ import annotations

import concurrent.futures
import logging

from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.backend_runtime import _FACADE, _facade
from kiro_crew.apps.backend_runtime.ports import (
    _MAX_PORT,
    _MIN_PORT,
    PortUnavailableError,
    _claim_port,
)
from kiro_crew.apps.backend_runtime.registration import _app_enabled_state, _set_backend_health
from kiro_crew.apps.backend_runtime.stale_reap import _reap_stale_app_backends
from kiro_crew.apps.execution import app_execution_denied, shipped_builtin_app_root
from kiro_crew.apps.manager import _app_activation_denied, get_app_manifest, list_apps
from kiro_crew.sel import sel

logger = logging.getLogger(_FACADE)


# Ceiling on parallel boot spawns. Each one forks a sandboxed interpreter, so an
# unbounded fan-out on a host with many installed apps would trade boot latency
# for a CPU/memory spike at the worst possible moment.
_BOOT_SPAWN_MAX_WORKERS = 8


#: The one app backend that needs the gateway's ACTUALLY-bound port at spawn
#: (``KIROCREW_BOUND_PORT``): it reads live-target pointer state through an
#: in-gateway route. ``start_dashboard`` starts every other backend before
#: ``runner.setup()`` so an app's startup hooks find its backend running, and starts
#: this one only after ``_export_bound_port`` — the value does not exist before the bind.
DEV_FLEET_APP_NAME: str = "dev-fleet"


def start_enabled_app_backends() -> list[str]:
    """Start backends for all enabled apps that declare one.

    Called during gateway startup to restore app backends.
    Returns list of app names that were started.

    The :data:`DEV_FLEET_APP_NAME` backend is vetted and reconciled like
    every other app but NOT spawned here; ``start_dashboard`` starts it with
    :func:`start_deferred_app_backends` once the bound port exists.
    """
    # Reap app backends left running by a prior (e.g. SIGKILLed) gateway
    # generation before starting the new one. See the RFC,
    # "Apps as supervised sandboxed children".
    _reap_stale_app_backends()

    apps = list_apps()

    # Boot reconcile (regression fix): scrub global
    # mcp.json entries for any installed-but-NOT-enabled app that declares MCP servers.
    # A disabled app's backend is not running, so its HTTP MCP url points at a dead port;
    # left in ~/.kiro/settings/mcp.json it breaks EVERY kiro session (connect failure →
    # "transient 5xx" → 3 retries → hard error). Enable's deregister can be missed (crash
    # mid-enable, a resources-mismatch branch), so reconcile at boot before starting any
    # backend. Enabled apps are (re)registered with their live port via the health-gate.
    for app_info in apps:
        if app_info.get("enabled"):
            continue
        name = app_info.get("name", "")
        manifest = app_info.get("manifest", {})
        if not name or not manifest.get("mcpServers"):
            continue
        try:
            # circular import: bridges imports from backend, so defer to call time.
            from kiro_crew.apps.bridges import _deregister_mcp_servers

            removed = _deregister_mcp_servers(name)
            if removed:
                logger.info(
                    "Boot reconcile: scrubbed %d stale MCP server(s) for disabled app %s",
                    removed,
                    name,
                )
        except Exception as exc:  # noqa: BLE001 — boot must never crash on reconcile
            logger.warning("Boot MCP reconcile failed for disabled app %s: %s", name, exc)

    # Executable-resource reconcile: restore agents, skills, cron definitions,
    # and MCP config only for apps admitted by every activation boundary. A
    # policy tightened after install must revoke stale derivative resources,
    # not merely decline to start the backend.
    for app_info in apps:
        if not app_info.get("enabled"):
            continue
        name = app_info.get("name", "")
        try:
            from kiro_crew.apps.bridges import (
                _deregister_agents,
                _deregister_mcp_servers,
                _deregister_skills,
                reconcile_app_skills,
                register_app,
            )
        except Exception as exc:  # noqa: BLE001 — boot must never crash on reconcile
            logger.warning("Boot resource reconcile unavailable: %s", exc)
            break

        # Governance/admission/execution vetting is deny-by-default. Builtins
        # remain exempt from signature/allowlist admission, but their execution
        # exemption still requires immutable shipped name + path provenance.
        try:
            gov_denied = _app_activation_denied(name)
            adm_denied = None
            if not gov_denied and app_info.get("origin") != "builtin":
                adm_denied = app_admission_denied(
                    name, manifest=get_app_manifest(name), action="boot"
                )
            execution_denied = None
            if not gov_denied and not adm_denied:
                execution_denied = app_execution_denied(
                    name,
                    action="resource_boot_reconcile",
                    app_root=shipped_builtin_app_root(name),
                    caller="gateway",
                )
        except Exception as exc:  # noqa: BLE001 — vetting error == denial
            gov_denied = f"governance/admission/execution vetting raised: {exc}"
            adm_denied = None
            execution_denied = None

        denied = gov_denied or adm_denied or execution_denied
        if denied:
            try:
                _deregister_agents(name)
                _deregister_skills(name)
                _deregister_mcp_servers(name)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Boot resource reconcile: FAILED to revoke resources for " "denied app %s: %s",
                    name,
                    exc,
                )
            else:
                logger.warning(
                    "Boot resource reconcile: revoked executable resources for "
                    "denied app %s: %s",
                    name,
                    denied,
                )
            continue

        try:
            registration = register_app(name)
            if registration.errors:
                logger.warning(
                    "Boot resource reconcile for app %s completed with errors: %s",
                    name,
                    registration.errors,
                )
            reconcile_app_skills(name)
        except Exception as exc:  # noqa: BLE001 — boot must never crash on reconcile
            logger.warning("Boot resource reconcile failed for app %s: %s", name, exc)

    # Vet first, then spawn the admitted set CONCURRENTLY. Vetting is cheap and
    # order-dependent bookkeeping; spawning is the slow part (each child is polled
    # for a grace window), so serializing it would make boot latency scale linearly
    # with the number of installed apps.
    admitted: list[str] = []
    for app_info in apps:
        if not app_info.get("enabled"):
            continue
        name = app_info.get("name", "")
        # Governance: the ``apps`` allowlist is an activation ceiling, so it must
        # gate startup re-activation too — not just the manual enable transition.
        # A policy tightened AFTER an app was enabled would otherwise let the app
        # load on the next restart (its persisted enabled=true bypasses the
        # enable_app gate). Re-vet here so a now-forbidden app stays down.
        gov_denied = _app_activation_denied(name)
        if gov_denied:
            logger.warning("App %s not started: blocked by governance policy: %s", name, gov_denied)
            continue
        manifest = app_info.get("manifest", {})
        if not manifest.get("backend", {}).get("entryPoint"):
            continue
        # Re-vet admission at boot: an app enabled before a policy tightened
        # (banned / allowlist-removed / now-unsigned) must NOT keep running
        # across restarts. Builtins (origin == "builtin") are trusted first-party
        # code shipped unsigned, so they are exempt (same carve-out as enable_app)
        # — otherwise a require_signature policy would strand every core app.
        if app_info.get("origin") != "builtin":
            try:
                denied = app_admission_denied(name, manifest=get_app_manifest(name), action="boot")
            except Exception as exc:  # noqa: BLE001 — boot must never crash on re-vet
                # Fail CLOSED: if the re-vet itself errors (transient I/O, a bug
                # in the admission logic), treat the app as denied rather than
                # booting it unchecked. The loop still continues to the next app,
                # so a single failure never crashes boot — it just declines to
                # start the app whose admission we could not confirm.
                logger.error(
                    "Boot admission re-vet failed for app %s: %s — treating as denied "
                    "(fail-closed)",
                    name,
                    exc,
                )
                denied = f"admission re-vet error: {exc}"
            if denied:
                logger.warning(
                    "Boot: skipping enabled app %s — blocked by admission policy: %s",
                    name,
                    denied,
                )
                try:
                    sel().log_api_access(
                        caller="gateway",
                        operation="app_backend_boot",
                        outcome="denied",
                        resources=name,
                        error=denied,
                    )
                except Exception as exc:
                    logger.debug("SEL audit failed for app %s boot deny: %s", name, exc)
                continue
        admitted.append(name)

    global _DEV_FLEET_DEFERRED
    _DEV_FLEET_DEFERRED = DEV_FLEET_APP_NAME in admitted
    return _start_backends_concurrently([n for n in admitted if n != DEV_FLEET_APP_NAME])


#: Whether the boot wave admitted Dev Fleet and held its spawn back for the bound port.
_DEV_FLEET_DEFERRED: bool = False


def start_deferred_app_backends() -> list[str]:
    """Spawn the Dev Fleet backend ``start_enabled_app_backends`` held back.

    Its admission and reconcile work already ran in the same boot, but the deferral
    leaves a window (the rest of ``start_dashboard``) in which the operator can
    disable the app or a policy can tighten — so enablement and governance are
    re-checked here, fail-closed, immediately before the spawn. Same per-app
    isolation as the main wave. Returns the names that started; a second call is a
    no-op.
    """
    global _DEV_FLEET_DEFERRED
    from kiro_crew.apps.manager import _app_activation_denied

    deferred, _DEV_FLEET_DEFERRED = _DEV_FLEET_DEFERRED, False
    if not deferred:
        return []
    name = DEV_FLEET_APP_NAME
    # ``True`` only: an unreadable state (None) is not a licence to spawn.
    if _app_enabled_state(name) is not True:
        logger.info("Deferred boot: %s is no longer enabled — not started", name)
        return []
    try:
        gov_denied = _app_activation_denied(name)
    except Exception as exc:  # noqa: BLE001 — fail closed, never crash boot
        gov_denied = f"activation re-vet error: {exc}"
    if gov_denied:
        logger.warning("Deferred boot: %s not started: %s", name, gov_denied)
        return []
    return _start_backends_concurrently([name])


def _preclaim_fixed_ports(names: list[str]) -> None:
    """Reserve every declared fixed port before concurrent spawns are submitted.

    Best-effort and non-fatal: an unreadable manifest or an out-of-range/duplicate
    port is simply left to the spawn itself, which already validates and reports
    it. This only removes the ordering hazard; it never decides whether an app may
    start.
    """

    for name in names:
        try:
            manifest = get_app_manifest(name)
            if manifest is None:
                continue
            port_str = str(manifest.backend.port)
            if not port_str or port_str == "auto":
                continue
            port = int(port_str)
        except (AttributeError, TypeError, ValueError):
            continue
        if not (_MIN_PORT <= port <= _MAX_PORT):
            continue
        try:
            _claim_port(name, port)
        except PortUnavailableError as exc:
            # Two apps declaring the same fixed port: a real conflict the spawn
            # path reports per app. Log once here for the boot-time picture.
            logger.warning("Boot: fixed-port pre-claim for app %s skipped: %s", name, exc)


def _start_backends_concurrently(names: list[str]) -> list[str]:
    """Spawn the given app backends in parallel; return those that started.

    Each app's spawn blocks on a survival grace window, so starting them one at a
    time would cost roughly N x that window. They are independent (ports are
    reserved atomically — see ``_reserve_free_port``), so they run concurrently,
    ``_BOOT_SPAWN_MAX_WORKERS`` at a time: boot costs about one window per wave
    rather than one per app.

    Declared FIXED ports are reserved up front, before any spawn is submitted.
    A fixed port is a requirement, not a preference, so it must not be lost to an
    auto-port app that merely happened to select it first — pre-claiming removes
    that race entirely, leaving `PortUnavailableError` to signal only a genuine
    conflict (two apps declaring the same port, or a foreign holder).

    Failure isolation is per app: one app's spawn raising or returning None must
    never take down the gateway (Slack + dashboard + every session) or affect the
    other apps.
    """

    if not names:
        return []

    _preclaim_fixed_ports(names)

    started: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(names), _BOOT_SPAWN_MAX_WORKERS),
        thread_name_prefix="app-boot",
    ) as pool:
        futures = {pool.submit(_facade().start_app_backend, name): name for name in names}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                ap = future.result()
            except Exception as exc:  # noqa: BLE001 — boot must never crash on one app
                # A per-app spawn failure (e.g. sandbox.wrap_argv fail-closing when
                # no OS-level sandbox backend is available — macOS 26 removed
                # sandbox-exec) must NOT take down the whole gateway. Log, audit,
                # and skip this app — same fail-isolated posture as the admission
                # re-vet and MCP reconcile branches above.
                logger.error(
                    "Boot: failed to start backend for app %s: %s — skipping "
                    "(gateway continues)",
                    name,
                    exc,
                )
                try:
                    sel().log_api_access(
                        caller="gateway",
                        operation="app_backend_boot",
                        outcome="error",
                        resources=name,
                        error=str(exc),
                    )
                except Exception as sel_exc:
                    logger.debug("SEL audit failed for app %s boot error: %s", name, sel_exc)
                continue
            if ap:
                started.append(name)
                logger.info("Auto-started backend for app %s on port %d", name, ap.port)
                # MCP re-registration is HEALTH-GATED: the health-check loop started
                # by start_app_backend calls _gate_mcp_registration once /health
                # passes, writing the HTTP MCP url with the real allocated port
                # (which may differ from the manifest's illustrative port).
                # Registering here — before health — is exactly what could leave a
                # dead url for an enabled-but-never-healthy app and break every
                # kiro-cli session. EXCEPTION: an adopted already-healthy instance
                # runs no health loop, so register it synchronously now.
                # Routed through the shared transition so this shares one order with
                # the watch that _start_adopted_health_watch has by now armed on the
                # same record — the two must not interleave their mcp.json writes.
                if ap.healthy:
                    _set_backend_health(ap, healthy=True)
    return started
