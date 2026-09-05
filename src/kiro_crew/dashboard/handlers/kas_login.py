"""Authenticated dashboard handlers for KAS-mode interactive login."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from aiohttp import web

from kiro_crew.auth.login.builder_id import BuilderIdAuthError
from kiro_crew.auth.login.device import DeviceAuthError
from kiro_crew.auth.service import (
    InvalidRegionError,
    KasLoginService,
    LoopbackUnavailableError,
    MissingStartUrlError,
    UnknownLoginError,
)
from kiro_crew.auth.store import TokenStore, TokenStoreError
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


def _service(request: web.Request) -> KasLoginService | None:
    """Resolve the KAS login service, creating it lazily on first use.

    Deliberately NOT constructed at gateway boot: KAS mode is pre-integration, so
    an API-only launch must not pay its init on the socket-readiness path (the
    ``no-new-work-on-gateway-boot-path`` rule). The first KAS request builds it and
    stashes it on the app so later requests reuse it and shutdown can close it.
    A pre-seeded instance (e.g. a test) is honored as-is.
    """
    service = request.app.get("kas_login_service")
    if isinstance(service, KasLoginService):
        return service
    try:
        service = KasLoginService(TokenStore(data_home()))
    except Exception:
        logger.warning("could not construct KasLoginService", exc_info=True)
        return None
    request.app["kas_login_service"] = service
    return service


def _unavailable() -> web.Response:
    return web.json_response(
        {"error": "KAS login service unavailable.", "code": "kas_login_unavailable"},
        status=503,
    )


async def _require_owner(request: web.Request, operation: str) -> web.Response | None:
    """Refuse a non-owner caller for a credential mutation.

    The KAS token store is a single machine-global Kiro credential, so a device
    login or logout replaces/deletes it for the whole gateway. Only the dashboard
    owner may drive those; a non-owner allowed user gets an audited, coded 403.
    Returns the 403 response to short-circuit with, or None when the caller is owner.
    """
    if is_owner_dashboard_request(request):
        return None
    await _audit(request, operation, "denied", error="non-owner caller")
    return web.json_response(
        {"error": "Only the dashboard owner can manage Kiro sign-in.", "code": "owner_only"},
        status=403,
    )


def _caller(request: web.Request) -> str:
    user = request.get("user", "")
    return str(user) if user else "dashboard-user"


async def _read_json(request: web.Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


async def _audit(request: web.Request, operation: str, outcome: str, error: str = "") -> None:
    """SEL-audit a login mutation; a broken audit sink must not break login itself."""

    def _log() -> None:
        sel().log_api_access(
            caller=_caller(request),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=request.path,
            error=error,
        )

    try:
        await asyncio.to_thread(_log)
    except Exception:
        logger.debug("Could not audit KAS login operation %s", operation, exc_info=True)


async def api_kas_login_status(request: web.Request) -> web.Response:
    """GET /api/kas-login — auth state + the transport this install shape supports."""
    service = _service(request)
    if service is None:
        return _unavailable()
    try:
        return web.json_response(await service.status())
    except TokenStoreError as err:
        # A refused (e.g. linked) token-store directory is a coded storage
        # failure, not an anonymous 500.
        await _audit(request, "kas_login_status", "failed", error=str(err))
        return web.json_response(
            {"error": "Could not read the stored sign-in.", "code": "token_store_failed"},
            status=500,
        )


async def api_kas_login_begin_device(request: web.Request) -> web.Response:
    """POST /api/kas-login/device {provider, start_url?, region?} — start a device login.

    ``start_url`` and ``region`` apply to the ``idc`` provider only (the company's
    IAM Identity Center portal); other providers ignore them.
    """
    denied = await _require_owner(request, "kas_login_begin_device")
    if denied is not None:
        return denied
    service = _service(request)
    if service is None:
        return _unavailable()
    body = await _read_json(request)
    provider = str((body or {}).get("provider") or "")
    start_url = str((body or {}).get("start_url") or "")
    region = str((body or {}).get("region") or "")
    if not provider:
        return web.json_response(
            {"error": "Missing 'provider'.", "code": "invalid_provider"}, status=400
        )
    try:
        result = await service.begin_device(provider, start_url=start_url, region=region)
    except ValueError:
        return web.json_response(
            {"error": f"Unknown provider: {provider}", "code": "invalid_provider"},
            status=400,
        )
    except MissingStartUrlError:
        return web.json_response(
            {"error": "Company SSO requires a start URL.", "code": "missing_start_url"},
            status=400,
        )
    except InvalidRegionError:
        return web.json_response(
            {"error": "Not a valid AWS region name.", "code": "invalid_region"},
            status=400,
        )
    except (DeviceAuthError, BuilderIdAuthError) as err:
        await _audit(request, "kas_login_begin_device", "failed", error=str(err))
        return web.json_response(
            {"error": str(err), "code": "device_authorization_failed"}, status=502
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        # Auth service offline / slow: return a coded 502, not an uncoded 500.
        await _audit(request, "kas_login_begin_device", "failed", error=str(err))
        return web.json_response(
            {"error": "Kiro auth service is unreachable.", "code": "auth_service_unreachable"},
            status=502,
        )
    await _audit(request, "kas_login_begin_device", "success")
    return web.json_response(result)


async def api_kas_login_poll(request: web.Request) -> web.Response:
    """POST /api/kas-login/poll {login_id} — one non-blocking poll of a pending login."""
    denied = await _require_owner(request, "kas_login_poll")
    if denied is not None:
        return denied
    service = _service(request)
    if service is None:
        return _unavailable()
    body = await _read_json(request)
    login_id = str((body or {}).get("login_id") or "")
    if not login_id:
        return web.json_response(
            {"error": "Missing 'login_id'.", "code": "missing_login_id"}, status=400
        )
    try:
        result = await service.poll_device(login_id)
    except UnknownLoginError:
        return web.json_response(
            {"error": "No pending login with that id.", "code": "unknown_login_id"},
            status=404,
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        # Auth service offline / slow during a poll: coded 502, not an uncoded 500.
        await _audit(request, "kas_login_poll", "failed", error=str(err))
        return web.json_response(
            {"error": "Kiro auth service is unreachable.", "code": "auth_service_unreachable"},
            status=502,
        )
    # Only the authorized transition persists a credential — that is the state
    # change worth an audit line; a pending tick is just a read.
    if result.get("status") == "authorized":
        await _audit(request, "kas_login_authorized", "success")
    return web.json_response(result)


async def api_kas_login_logout(request: web.Request) -> web.Response:
    """POST /api/kas-login/logout {identity} — delete one identity's stored token."""
    denied = await _require_owner(request, "kas_login_logout")
    if denied is not None:
        return denied
    service = _service(request)
    if service is None:
        return _unavailable()
    body = await _read_json(request)
    identity = str((body or {}).get("identity") or "")
    try:
        await service.logout(identity)
    except ValueError:
        return web.json_response(
            {"error": f"Unknown identity: {identity}", "code": "invalid_identity"},
            status=400,
        )
    except TokenStoreError as err:
        # Unwritable/corrupt vault store: coded 500, not a false success.
        await _audit(request, "kas_login_logout", "failed", error=str(err))
        return web.json_response(
            {"error": "Could not remove the stored sign-in.", "code": "logout_failed"},
            status=500,
        )
    await _audit(request, "kas_login_logout", "success")
    return web.json_response({"ok": True})


async def api_kas_login_begin_loopback(request: web.Request) -> web.Response:
    """POST /api/kas-login/loopback {provider} — start a loopback (PKCE) sign-in.

    Returns the portal URL for the dashboard to open plus the polling handle. A
    coded 409 ``loopback_unavailable`` means "start the device flow instead": the
    install shape does not support loopback or every allowlisted port is busy.
    """
    denied = await _require_owner(request, "kas_login_begin_loopback")
    if denied is not None:
        return denied
    service = _service(request)
    if service is None:
        return _unavailable()
    body = await _read_json(request)
    provider = str((body or {}).get("provider") or "")
    if not provider:
        return web.json_response(
            {"error": "Missing 'provider'.", "code": "invalid_provider"}, status=400
        )
    try:
        result = await service.begin_loopback(provider)
    except ValueError:
        return web.json_response(
            {"error": f"Unknown provider: {provider}", "code": "invalid_provider"},
            status=400,
        )
    except LoopbackUnavailableError as err:
        await _audit(request, "kas_login_begin_loopback", "failed", error=str(err))
        return web.json_response(
            {"error": "Loopback sign-in is not available here.", "code": "loopback_unavailable"},
            status=409,
        )
    await _audit(request, "kas_login_begin_loopback", "success")
    return web.json_response(result)


async def api_kas_login_cancel(request: web.Request) -> web.Response:
    """POST /api/kas-login/cancel {login_id} — abandon a pending sign-in.

    Releases a loopback listener's callback port immediately instead of at its
    deadline. Idempotent, so the dashboard can call it on every start-over path.
    """
    denied = await _require_owner(request, "kas_login_cancel")
    if denied is not None:
        return denied
    service = _service(request)
    if service is None:
        return _unavailable()
    body = await _read_json(request)
    login_id = str((body or {}).get("login_id") or "")
    if not login_id:
        return web.json_response(
            {"error": "Missing 'login_id'.", "code": "missing_login_id"}, status=400
        )
    await service.cancel(login_id)
    return web.json_response({"ok": True})
