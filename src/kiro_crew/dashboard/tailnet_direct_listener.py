"""Start the direct tailnet listener beside the loopback one, or record why not.

The middleware chain is built before the bind with an empty gate holder
(:class:`~kiro_crew.dashboard.tailnet_direct.GateRef`); once the loopback
listener is up, the gate is resolved and the sites are bound on the same port. :func:`start` records the outcome for the Shared Crew capabilities and
adds the listener's own origins to the CSRF and ``Host`` allowlists.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew.config.resolution import tailnet_effective_allowed_logins, tailnet_identity_unknown
from kiro_crew.dashboard import tailnet, tailnet_direct
from kiro_crew.shared_crew import access

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

#: Why the listener does not run when the gateway is not bound to loopback.
REASON_BIND_NOT_LOOPBACK = "bind_not_loopback"
#: Why the listener did not run that still leaves a tailnet path needing the owner.
_OWNER_AFTER_START = (tailnet_direct.REASON_DISABLED, REASON_BIND_NOT_LOOPBACK)
#: The config :func:`resolve` saw, for the access-set install :func:`start` runs.
_PENDING_INSTALL: list[KiroCrewConfig] = []


def _install_access_set(cfg: KiroCrewConfig) -> None:
    """Publish Shared Crew's derived access set, seeding it once from the config list.

    An unreadable tailnet section seeds nothing (the seed is taken only from a
    list that was read), and a later start seeds from it then.
    """
    from kiro_crew.shared_crew import access_set

    unknown = tailnet_identity_unknown(cfg.degraded_sections)
    seed = None if unknown else tuple(cfg.dashboard.tailscale.allowed_logins)
    access_set.install(seed, local_authorization=cfg.dashboard.shared_crew.local_authorization)


def _resolve_blocking(
    cfg: KiroCrewConfig, port: int, local_only: bool
) -> tailnet_direct.DirectResolution:
    prepare_access_set(cfg)
    return _resolve_gate(cfg, port, local_only)


def prepare_access_set(cfg: KiroCrewConfig) -> None:
    """Before the bind: publish an owner-only placeholder set when a tailnet path is on.

    Reads nothing, so the boot path never waits on the store; a request before
    :func:`finish_access_set` runs is refused rather than judged by
    ``allowed_logins``. Both the dashboard and the headless API server call it.
    """
    _PENDING_INSTALL.clear()
    if not (cfg.dashboard.tailscale.enabled or cfg.dashboard.shared_crew.tailnet_direct):
        # No tailnet path is on: nothing reads the access set.
        return
    from kiro_crew.shared_crew import access_set

    access_set.install_placeholder(
        local_authorization=cfg.dashboard.shared_crew.local_authorization
    )
    _PENDING_INSTALL.append(cfg)


async def finish_access_set(resolution: tailnet_direct.DirectResolution | None = None) -> None:
    """After the bind: read the access set and learn the owner. Never raises."""
    try:
        await asyncio.to_thread(_install_after_bind, resolution)
    except Exception:  # noqa: BLE001 - the placeholder keeps refusing everyone but the owner
        logger.warning("shared crew access set setup after start failed", exc_info=True)


def _publish_owner(owner: tailnet_direct.SelfNode | None) -> None:
    if owner is not None and owner.login and owner.user_id:
        from kiro_crew.shared_crew import access_set

        access_set.set_owner(owner.login, owner.user_id)


def _learn_owner_from_daemon() -> None:
    """Ask the daemon who owns this machine. Blocking; run after the dashboard binds."""
    _publish_owner(tailnet_direct.parse_status(tailnet_direct.tailnet_localapi.status().data))


def _install_after_bind(resolution: tailnet_direct.DirectResolution | None) -> None:
    """Read the access set and learn the owner, once the dashboard answers. Blocking."""
    if not _PENDING_INSTALL:
        return
    cfg = _PENDING_INSTALL.pop()
    try:
        _install_access_set(cfg)
    except Exception:  # noqa: BLE001 - the placeholder keeps refusing everyone but the owner
        logger.warning("shared crew access set could not be installed", exc_info=True)
    if resolution is not None and resolution.gate is not None:
        _publish_owner(resolution.gate.owner)
    elif resolution is None or resolution.reason in _OWNER_AFTER_START:
        # The owner is admitted by every tailnet path, not just this listener.
        _learn_owner_from_daemon()


def _resolve_gate(
    cfg: KiroCrewConfig, port: int, local_only: bool
) -> tailnet_direct.DirectResolution:
    if not cfg.dashboard.shared_crew.tailnet_direct:
        return tailnet_direct.DirectResolution(
            tailnet_direct.STATE_OFF, tailnet_direct.REASON_DISABLED
        )
    if not local_only:
        # A widened bind already accepts tailnet traffic on its own socket;
        # the direct listener runs only beside the loopback-only default.
        return tailnet_direct.DirectResolution(
            tailnet_direct.STATE_UNAVAILABLE, REASON_BIND_NOT_LOOPBACK
        )
    ts = cfg.dashboard.tailscale
    return tailnet_direct.resolve(
        port=port,
        allowed_logins=tailnet_effective_allowed_logins(cfg.degraded_sections, ts.allowed_logins),
        identity_unknown=tailnet_identity_unknown(cfg.degraded_sections),
        governance_pinned=tailnet.is_governance_pinned_off(audit_tool="tailnet_direct_startup"),
    )


async def resolve(
    cfg: KiroCrewConfig, port: int, local_only: bool
) -> tailnet_direct.DirectResolution:
    """Decide, off the event loop, whether the direct listener runs. Never raises."""
    try:
        resolution = await asyncio.to_thread(_resolve_blocking, cfg, port, local_only)
    except Exception:  # noqa: BLE001 - the listener is an addition; its failure adds nothing
        logger.warning("tailnet direct listener could not be resolved", exc_info=True)
        resolution = tailnet_direct.DirectResolution(
            tailnet_direct.STATE_UNAVAILABLE, tailnet_direct.REASON_TAILSCALE_NOT_RUNNING
        )
    _note(resolution, ())
    if resolution.state != tailnet_direct.STATE_ACTIVE:
        logger.info(
            "tailnet direct listener %s (%s)", resolution.state, resolution.reason or "no reason"
        )
    return resolution


def _note(resolution: tailnet_direct.DirectResolution, urls: tuple[str, ...]) -> None:
    gate = resolution.gate
    access.note_tailnet_direct(
        access.DirectTailnet(
            state=resolution.state,
            reason=resolution.reason,
            method=resolution.method,
            urls=urls,
            owner_login=gate.owner.login if gate is not None else "",
            addresses=gate.owner.addresses if gate is not None else (),
        )
    )


async def start(
    app: web.Application,
    runner: web.AppRunner,
    bound_port: int,
    resolution: tailnet_direct.DirectResolution,
    ref: tailnet_direct.GateRef | None = None,
) -> list[web.BaseSite]:
    """Bind the direct sites for an active *resolution* and publish the result.

    *ref* is the holder the middleware chain reads; it gets the gate before the
    first direct site accepts a connection, and loses it if none bound.
    """
    gate = resolution.gate
    await finish_access_set(resolution)
    if resolution.state != tailnet_direct.STATE_ACTIVE or gate is None:
        return []
    gate.port = bound_port
    if ref is not None:
        ref.gate = gate
    sites = await tailnet_direct.start_sites(runner, bound_port, gate)
    if not sites:
        if ref is not None:
            ref.gate = None
        _note(
            tailnet_direct.DirectResolution(
                tailnet_direct.STATE_UNAVAILABLE,
                tailnet_direct.REASON_BIND_FAILED,
                gate=gate,
                method=resolution.method,
            ),
            (),
        )
        return []
    bound = {site.name for site in sites}
    origins = tailnet_direct.direct_origins(gate, bound_port)
    allowed = app.get("allowed_origins")
    if isinstance(allowed, set):
        allowed.update(origins)
    urls = tuple(tailnet_direct.direct_urls(gate, bound_port))
    _note(resolution, urls)
    logger.info("tailnet direct listener active on %s", ", ".join(sorted(bound)))
    return sites


def prepare_at_start(
    cfg: KiroCrewConfig,
    *,
    host: str,
    trust: tailnet.TailnetTrust,
    local_only: bool,
) -> tailnet_direct.GateRef:
    """Before the bind: record the tailnet path and return the chain's empty gate holder.

    ``start_dashboard`` calls this once it knows this machine's tailnet name and
    identity trust, before it builds the middleware chain. It reads nothing: the
    daemon read that decides the gate runs in :func:`start_after_bind`, and the
    proxy-proof probe after the bind too (``host_power.probe_after_start``), so
    the path is recorded with no proof gap here.
    """
    access.note_tailnet(
        enabled=bool(cfg.dashboard.tailscale.enabled),
        host=host,
        identity_trust=trust.enforces_identity and not trust.identity_unknown,
        loopback_only=local_only,
        proof_gap=None,
        identity_config_unreadable=tailnet_identity_unknown(cfg.degraded_sections),
    )
    prepare_access_set(cfg)
    return tailnet_direct.GateRef()


async def start_after_bind(
    app: web.Application,
    runner: web.AppRunner,
    bound_port: int,
    cfg: KiroCrewConfig,
    ref: tailnet_direct.GateRef,
    *,
    local_only: bool,
) -> list[web.BaseSite]:
    """After the bind: :func:`resolve` the gate, then :func:`start` its sites into *ref*."""
    resolution = await resolve(cfg, bound_port, local_only)
    return await start(app, runner, bound_port, resolution, ref)


__all__ = [
    "REASON_BIND_NOT_LOOPBACK",
    "finish_access_set",
    "prepare_access_set",
    "prepare_at_start",
    "resolve",
    "start",
    "start_after_bind",
]
