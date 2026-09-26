"""App Context — scoped access to gateway services for app hooks and routes.

The AppContext is created per-app at enable time and injected into route
handlers and lifecycle hooks. It provides a controlled surface area — apps
interact with gateway services only through this interface.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.apps.app_storage import AppStorage
from kiro_crew.apps.audit_sdk import AuditSDK
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.apps.event_bus import EventBus
from kiro_crew.apps.job_sdk import JobSDK
from kiro_crew.apps.scrub_sdk import ScrubSDK
from kiro_crew.apps.spawn_sdk import SpawnSDK


@dataclass
class AppHealthStatus:
    """Tracks the health of an app's subsystems after enable."""

    status: str = "healthy"  # "healthy" | "degraded" | "error"
    issues: list[str] = field(default_factory=list)
    last_checked: str = ""  # ISO 8601

    def mark_degraded(self, issue: str) -> None:
        """Mark a non-critical subsystem failure."""
        self.status = "degraded"
        self.issues.append(issue)
        self.last_checked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def mark_error(self, issue: str) -> None:
        """Mark a critical failure."""
        self.status = "error"
        self.issues.append(issue)
        self.last_checked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status}
        if self.issues:
            d["issues"] = self.issues
        if self.last_checked:
            d["last_checked"] = self.last_checked
        return d


@dataclass
class AppContext:
    """Scoped context passed to app hooks and route handlers.

    Only services declared in the app's permissions are populated;
    others are None.
    """

    name: str
    data_dir: Path
    config: dict[str, Any] = field(default_factory=dict)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("kirocrew.app"))
    cron: CronSDK | None = None
    events: EventBus | None = None
    storage: AppStorage | None = None
    spawn: SpawnSDK | None = None
    job: JobSDK | None = None
    health: AppHealthStatus = field(default_factory=AppHealthStatus)
    # NOT `| None`, unlike every SDK above: those grant a capability and stay None
    # until the manifest asks for it, while this one only removes sensitive data
    # from a string the app already holds. An app that had to check for None would
    # get a silent no-redaction path on the else branch, and an app refused the
    # seam would ship its own regexes -- the outcome the seam exists to prevent.
    # See scrub_sdk.ScrubSDK. The default needs no app name to invent because the
    # SDK carries none, so a hand-built context gets the same scrubber the factory
    # would hand it rather than one attributed to an app that does not exist.
    scrub: ScrubSDK = field(default_factory=ScrubSDK)
    # `| None` like the capability SDKs above and UNLIKE `scrub`, though nothing
    # gates it either: a default instance would have to invent an app name, and a
    # row attributed to an app that does not exist defeats the attribution this
    # SDK is for -- whereas a scrubber carries no identity, so it can have one.
    # `build_app_context` always populates it, so a context built by the factory
    # never sees None.
    audit: AuditSDK | None = None
    # The gateway's own aiohttp Application, for an app whose background work has
    # to be anchored on it -- a poller that reads the dashboard state its request
    # handlers already read, and stashes the running service where those handlers
    # look it up. Typed ``Any`` so this module keeps its aiohttp-free import.
    #
    # Populated ONLY for an app that declares a ``routes`` hook, and that gate is
    # what keeps this from being a new grant: such an app is handed the real
    # ``web.Request`` on every call, so ``request.app`` is already this same
    # object. An app with only lifecycle hooks gets ``None`` and must say so
    # rather than guess -- hence ``| None`` and no default instance.
    http_app: Any | None = None


def manifest_declares_routes(manifest: dict[str, Any]) -> bool:
    """Whether *manifest* declares a ``routes`` hook, read defensively.

    The grant decision itself, separated from the handle so a caller that must
    REMEMBER the answer can store a bool rather than a live Application: a cached
    object would pin the gateway's Application past its own lifetime, and a cached
    ``None`` could not be told apart from "this app was never granted".
    """
    return bool(manifest.get("backend", {}).get("hooks", {}).get("routes"))


def http_app_for_manifest(manifest: dict[str, Any], http_app: Any) -> Any:
    """The Application *manifest*'s app may carry on its context, or ``None``.

    ONE predicate, called by every builder, because the hazard here is two
    builders disagreeing: a startup context that carries the handle and a
    shutdown context that does not means an app can start background work it can
    never be asked to stop, and the teardown hook that exists to stop it sees
    ``None`` and returns as if there were nothing to do.

    The grant is decided by the app declaring a ``routes`` hook, and that is what
    keeps it from being a new grant at all -- see :attr:`AppContext.http_app`.

    Reading the CURRENT manifest is right only while the two contexts are built
    from one enable. A teardown that may run against a manifest the app has since
    rewritten must instead reuse the answer recorded at enable time -- see
    ``module_loader.cache_http_app_grant``.
    """
    return http_app if manifest_declares_routes(manifest) else None


def build_app_context(
    app_name: str,
    data_dir: Path,
    *,
    permissions: dict[str, Any] | None = None,
    cron_service: Any = None,
    broadcast_fn: Any = None,
    spawn_impl: Any = None,
    app_config: dict[str, Any] | None = None,
    http_app: Any = None,
) -> AppContext:
    """Factory that builds an AppContext based on app permissions.

    Args:
        app_name: The app's identifier.
        data_dir: The app's data directory path.
        permissions: The app's declared permissions dict from manifest.
        cron_service: The gateway's CronService instance (for CronSDK).
        broadcast_fn: The gateway's broadcast function (for EventBus).
        app_config: App-specific configuration dict.
        http_app: The gateway's aiohttp Application, or None. Passed straight
            through, NOT gated on a permission: the caller decides, because the
            decision is whether this app already reaches the object by another
            route (see ``AppContext.http_app``) and only the wiring knows that.

    Returns:
        AppContext with services populated based on permissions.
    """
    perms = permissions or {}
    ctx_logger = logging.getLogger(f"kirocrew.app.{app_name}")

    # Build CronSDK if permitted
    cron_sdk = None
    if perms.get("cron") and cron_service is not None:
        cron_sdk = CronSDK(app_name, cron_service)

    # Build EventBus if permitted
    event_bus = None
    events_list = perms.get("events", [])
    if events_list and broadcast_fn is not None:
        event_bus = EventBus(app_name, events_list, broadcast_fn)

    # Build SpawnSDK if permitted. Same shape as the others: no permission or
    # no host implementation -> None, and the app's own guard reports it.
    spawn_sdk = None
    if perms.get("spawn") is True and spawn_impl is not None:
        # The completion probe rides on the impl callable (see build_spawn_impl)
        # so the spawn wiring stays a single parameter end to end.
        spawn_sdk = SpawnSDK(
            app_name, spawn_impl, done_probe=getattr(spawn_impl, "done_probe", None)
        )

    # Build AppStorage if permitted
    app_storage = None
    if perms.get("storage"):
        app_storage = AppStorage(app_name, data_dir)

    # Build JobSDK if permitted. Unlike cron/spawn there is no host service to
    # inject: the run store IS the app's own data dir, so the permission alone
    # decides. Registering it in the process-wide lookup the shared routes read
    # is deliberately NOT done here -- that is the gateway wiring's job (see
    # hooks_integration._build_app_context_from_info), so this factory stays
    # free of process-global side effects and a test can build a context
    # without publishing an SDK to every route.
    job_sdk = None
    if perms.get("jobs"):
        job_sdk = JobSDK(app_name, data_dir)

    return AppContext(
        name=app_name,
        data_dir=data_dir,
        config=app_config or {},
        logger=ctx_logger,
        cron=cron_sdk,
        events=event_bus,
        storage=app_storage,
        spawn=spawn_sdk,
        job=job_sdk,
        # Both unconditional: see the fields' comments on AppContext.
        scrub=ScrubSDK(),
        audit=AuditSDK(app_name),
        http_app=http_app,
    )
