"""Local machine: the local gateway's consent and control routes.

Granting needs NATIVE consent on this machine, in two halves (the external
terminal's grant shape, ``dashboard/handlers/external_terminal.py``):

1. ``POST /api/connected-computers/local/consent`` -- the local dashboard (owner,
   direct local request, local switch on) names a Remote Crew instance. It gets
   back a one-time consent id, valid :data:`CONSENT_TTL_SECS`, plus what the
   native dialog will show. Nothing is granted.
2. ``POST /api/connected-computers/local/consent/redeem`` -- STRICT internal path:
   only the desktop shell's main process (loopback + internal secret, no agent
   IPC capability) redeems the id, after ITS native confirmation and folder
   picker chose the root and the ticked MCP servers. Only then is the computer
   enrolled over SSH and the grant written.

A remote page (another gateway's dashboard, an iframe pane, anything reached
through a tunnel) cannot pass step 1 (not a direct local request) and never
holds the internal secret for step 2, so it cannot enable or widen a grant (B13).
A cancelled dialog redeems nothing and leaves no grant (B02).

Turning access OFF never needs the native path -- it only narrows:
``DELETE /api/connected-computers/local/grants/{grant_id}`` and
``POST /api/connected-computers/local/off`` take effect at once, switch on or off.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from typing import Any, Final, Optional

from aiohttp import web

from kiro_crew.connected_computers.flag import DISABLED_REASON, local_enabled

logger = logging.getLogger(__name__)

CONSENT_PATH: Final[str] = "/api/connected-computers/local/consent"
REDEEM_PATH: Final[str] = "/api/connected-computers/local/consent/redeem"
GRANTS_PATH: Final[str] = "/api/connected-computers/local/grants"
OFF_PATH: Final[str] = "/api/connected-computers/local/off"
#: The native dialog was cancelled: drop the consent id (it only narrows).
CANCEL_PATH: Final[str] = "/api/connected-computers/local/consent/cancel"
#: The native dialog reads what it shows here, never from the page.
DESCRIBE_PATH: Final[str] = "/api/connected-computers/local/consent/describe"
#: How long a consent id stays redeemable.
CONSENT_TTL_SECS: Final[float] = 120.0
_MAX_CONSENTS: Final[int] = 8

# consent id -> (instance id, monotonic expiry)
#: consent id -> (instance id, expiry, the Off generation read when it was minted).
_consents: dict[str, tuple[str, float, int]] = {}
_consents_lock = threading.Lock()


def _mint_consent(instance_id: str, off_generation: int) -> str:
    consent = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _consents_lock:
        for key in [k for k, (_i, exp, _g) in _consents.items() if exp <= now]:
            del _consents[key]
        while len(_consents) >= _MAX_CONSENTS:
            _consents.pop(next(iter(_consents)))
        _consents[consent] = (instance_id, now + CONSENT_TTL_SECS, off_generation)
    return consent


def take_consent(consent: str) -> Optional[tuple[str, int]]:
    """(instance, Off generation at mint) for a consent id, once; None when unknown or expired."""
    with _consents_lock:
        entry = _consents.pop(consent, None)
    if entry is None or entry[1] <= time.monotonic():
        return None
    return entry[0], entry[2]


def clear_consents() -> None:
    with _consents_lock:
        _consents.clear()


def _audit(operation: str, outcome: str, detail: str) -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="connected_computers",
            operation=operation,
            outcome=outcome,
            source="connected_computers",
            resources=detail,
        )
    except Exception:  # noqa: BLE001
        logger.debug("connected computers: audit failed", exc_info=True)


def _refuse(operation: str, status: int, code: str, error: str) -> web.Response:
    _audit(operation, "denied", code)
    return web.json_response({"error": error, "code": code}, status=status)


async def _owner(request: web.Request, operation: str) -> Optional[web.Response]:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    return await require_owner_dashboard_request(request, operation)


def _instance(instance_id: str) -> Any:
    from kiro_crew.instances.registry import InstancesRegistry

    return InstancesRegistry().get(instance_id)


async def api_local_consent(request: web.Request) -> web.Response:
    from kiro_crew.dashboard.handlers._shared import read_bounded_json
    from kiro_crew.dashboard.origin import is_direct_local_request

    op = "connected_computers.consent"
    if (refusal := await _owner(request, op)) is not None:
        return refusal
    if not is_direct_local_request(request):
        return _refuse(op, 403, "not_local", "only this computer can share itself")
    if not await asyncio.to_thread(local_enabled):
        return _refuse(op, 403, "connected_computers_disabled", DISABLED_REASON)
    body, error = await read_bounded_json(request, max_bytes=2048)
    if error is not None:
        return error
    assert body is not None
    instance_id = body.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        return _refuse(op, 400, "invalid_request", "instance_id is required")
    instance = await asyncio.to_thread(_instance, instance_id)
    if instance is None or not getattr(instance, "ssh_host", ""):
        return _refuse(op, 404, "unknown_instance", "no Remote Crew connection with SSH by that id")
    from kiro_crew.connected_computers.controller import get_controller
    from kiro_crew.connected_computers.local_mcp import configured_servers, exposable
    from kiro_crew.connected_computers.share import ShareRefused, read_off_generation

    # Read before the native dialog opens: an Off made while it is open refuses
    # the grant this consent would write.
    try:
        generation = await read_off_generation(get_controller().store)
    except ShareRefused as exc:
        return _refuse(op, exc.status, exc.code, exc.message)
    servers = await asyncio.to_thread(configured_servers)
    consent = _mint_consent(instance_id, generation)
    _audit(op, "ok", f"instance={instance_id}")
    return web.json_response(
        {
            "consent": consent,
            "expires_in": CONSENT_TTL_SECS,
            "instance": {"id": instance_id, "name": getattr(instance, "name", "") or instance_id},
            "mcp": exposable(servers),
        }
    )


def _native_refusal(request: web.Request, op: str) -> Optional[web.Response]:
    """None only for the desktop shell's main process (loopback + internal secret)."""
    from kiro_crew.dashboard.agent_ipc import GRANT_KEY
    from kiro_crew.dashboard.urls import is_loopback

    if request.get("internal_auth") is not True or not is_loopback(request.remote or ""):
        return _refuse(op, 403, "internal_secret_required", "forbidden")
    if request.get(GRANT_KEY) is not None:
        # An agent's IPC capability never stands in for the desktop shell.
        return _refuse(op, 403, "internal_secret_required", "forbidden")
    return None


def peek_consent(consent: str) -> Optional[str]:
    """The instance a live consent id names, without spending it."""
    with _consents_lock:
        entry = _consents.get(consent)
    if entry is None or entry[1] <= time.monotonic():
        return None
    return entry[0]


async def api_local_consent_describe(request: web.Request) -> web.Response:
    """What the native dialog shows, read from the gateway rather than the page (B13)."""
    from kiro_crew.connected_computers.local_mcp import configured_servers, exposable

    op = "connected_computers.consent.describe"
    if (refusal := _native_refusal(request, op)) is not None:
        return refusal
    if not await asyncio.to_thread(local_enabled):
        return _refuse(op, 403, "connected_computers_disabled", DISABLED_REASON)
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    consent = body.get("consent") if isinstance(body, dict) else None
    instance_id = peek_consent(consent) if isinstance(consent, str) else None
    if instance_id is None:
        return _refuse(op, 404, "no_consent", "no such consent; start again")
    instance = await asyncio.to_thread(_instance, instance_id)
    if instance is None:
        return _refuse(op, 404, "unknown_instance", "that Remote Crew connection is gone")
    servers = await asyncio.to_thread(configured_servers)
    return web.json_response(
        {
            "instance": {"id": instance_id, "name": getattr(instance, "name", "") or instance_id},
            "mcp": exposable(servers),
        }
    )


async def api_local_consent_cancel(request: web.Request) -> web.Response:
    """The native dialog was cancelled: the consent id cannot be redeemed after this (B02)."""
    op = "connected_computers.consent.cancel"
    if (refusal := _native_refusal(request, op)) is not None:
        return refusal
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    consent = body.get("consent") if isinstance(body, dict) else None
    if not isinstance(consent, str) or not consent:
        return _refuse(op, 400, "invalid_request", "consent is required")
    take_consent(consent)
    _audit(op, "ok", "cancelled")
    return web.json_response({"cancelled": True})


async def api_local_consent_redeem(request: web.Request) -> web.Response:
    from kiro_crew.connected_computers.controller import get_controller
    from kiro_crew.connected_computers.share import ShareRefused, share_computer

    op = "connected_computers.consent.redeem"
    if (refusal := _native_refusal(request, op)) is not None:
        return refusal
    if not await asyncio.to_thread(local_enabled):
        return _refuse(op, 403, "connected_computers_disabled", DISABLED_REASON)
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        return _refuse(op, 400, "invalid_request", "a JSON object is required")
    consent, root, mcp = body.get("consent"), body.get("root"), body.get("mcp", [])
    taken = take_consent(consent) if isinstance(consent, str) else None
    if taken is None:
        return _refuse(op, 404, "no_consent", "no such consent; start again")
    instance_id, generation = taken
    if not isinstance(mcp, list) or not all(isinstance(n, str) for n in mcp):
        return _refuse(op, 400, "invalid_request", "mcp must be a list of names")
    controller = get_controller()
    try:
        grant = await share_computer(
            controller,
            instance_id=instance_id,
            # Read at call time, so the module's own switch and registry apply.
            instance_lookup=lambda iid: _instance(iid),
            root=root,
            mcp=mcp,
            enabled=lambda: local_enabled(),
            off_generation=generation,
        )
    except ShareRefused as exc:
        return _refuse(op, exc.status, exc.code, exc.message)
    _audit(op, "ok", f"instance={instance_id} device={grant.device_id}")
    return web.json_response({"grant": grant.public()})


async def api_local_grants(request: web.Request) -> web.Response:
    from kiro_crew.connected_computers.controller import get_controller

    if (refusal := await _owner(request, "connected_computers.grants")) is not None:
        return refusal
    rows = await asyncio.to_thread(get_controller().status)
    enabled = await asyncio.to_thread(local_enabled)
    return web.json_response({"grants": rows, "enabled": enabled})


async def api_local_grant_revoke(request: web.Request) -> web.Response:
    from kiro_crew.connected_computers.controller import get_controller

    op = "connected_computers.revoke"
    if (refusal := await _owner(request, op)) is not None:
        return refusal
    grant = await get_controller().revoke(request.match_info["grant_id"])
    if grant is None:
        return _refuse(op, 404, "unknown_grant", "no such grant")
    _audit(op, "ok", f"grant={grant.grant_id}")
    return web.json_response({"revoked": grant.grant_id})


async def api_local_off(request: web.Request) -> web.Response:
    from kiro_crew.connected_computers.controller import get_controller

    op = "connected_computers.off"
    if (refusal := await _owner(request, op)) is not None:
        return refusal
    ended = await get_controller().off()
    _audit(op, "ok", f"grants={len(ended)}")
    return web.json_response({"revoked": [g.grant_id for g in ended]})


def register_routes(app: web.Application) -> None:
    app.router.add_post(CONSENT_PATH, api_local_consent)
    app.router.add_post(REDEEM_PATH, api_local_consent_redeem)
    app.router.add_post(CANCEL_PATH, api_local_consent_cancel)
    app.router.add_post(DESCRIBE_PATH, api_local_consent_describe)
    app.router.add_get(GRANTS_PATH, api_local_grants)
    app.router.add_delete(GRANTS_PATH + "/{grant_id}", api_local_grant_revoke)
    app.router.add_post(OFF_PATH, api_local_off)
