"""Connect and disconnect routes for third-party secret backends.

One pair of routes serves every backend in :mod:`kiro_crew.secrets.backend`'s
registry: ``POST /api/secrets/backends/{id}`` with the backend's connect-form
values, ``DELETE /api/secrets/backends/{id}`` to forget the token. No route,
and no branch here, is specific to one provider. The listing half lives in
``GET /api/secrets`` (``secrets.api_secrets_list``).
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.handlers import secrets as secrets_handlers
from kiro_crew.secrets import SecretVault
from kiro_crew.secrets import backend as secret_backends
from kiro_crew.secrets import items

logger = logging.getLogger(__name__)


async def _gate(request: web.Request, operation: str) -> web.Response | None:
    """The owner gate, then the preview gate (off answers 404, as if no route)."""
    denied = await secrets_handlers._owner_only(request, operation)
    if denied is not None:
        return denied
    if not await asyncio.to_thread(secrets_handlers._secrets_vault_preview_on):
        return web.json_response(
            {"error": "Turn on the Secrets vault preview first", "code": "preview_off"},
            status=404,
        )
    return None


def _backend_or_404(request: web.Request) -> secret_backends.SecretBackend | web.Response:
    backend = secret_backends.get(request.match_info["backend_id"])
    if backend is None:
        return web.json_response(
            {"error": "No such password manager", "code": "unknown_backend"}, status=404
        )
    return backend


def _audit(request: web.Request, operation: str, outcome: str, resources: str) -> None:
    try:
        secrets_handlers._sel().log_api_access(
            caller=str(request.get("user") or "unknown"),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )
    except Exception:  # pragma: no cover — audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


async def api_secret_backend_connect(request: web.Request) -> web.Response:
    """POST /api/secrets/backends/{id} — connect a backend with its form's values.

    The values are checked against the provider, then stored in the local vault;
    a secret field is never echoed, logged or returned.
    """
    operation = "secrets_backend_connect"
    gated = await _gate(request, operation)
    if gated is not None:
        return gated
    backend = _backend_or_404(request)
    if isinstance(backend, web.Response):
        return backend
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "Invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "Request body must be a JSON object", "code": "invalid_body"}, status=400
        )
    vault = SecretVault(secrets_handlers.config_dir())
    try:
        values = secret_backends.form_values(backend, body)
        conn = await asyncio.to_thread(
            secret_backends.call, backend, lambda: backend.connect(values)
        )
    except secret_backends.BackendError as exc:
        _audit(request, operation, "failed", f"{backend.id}:{exc.code}")
        status = 400 if exc.code.startswith("invalid") else 409
        detail = await asyncio.to_thread(secret_backends.error_view, backend, exc)
        return web.json_response(
            {
                "error": str(exc),
                "code": exc.code,
                "message_key": detail.get("message_key", ""),
                "params": detail.get("params", {}),
                "install_command": detail.get("install_command", ""),
            },
            status=status,
        )
    try:
        await vault.set_item_with_revision(
            secret_backends.connection_item(backend.id),
            items.ITEM_TYPE_NOTE,
            secret_backends.connection_fields(conn),
        )
    except (OSError, items.ItemError):
        logger.warning("%s connection not stored: the vault write failed", backend.id)
        return web.json_response(
            {
                "error": "Could not save the connection to the vault. Check disk space "
                "and permissions, then try again.",
                "code": "vault_write_failed",
            },
            status=500,
        )
    _audit(request, operation, "ok", f"{backend.id}:{conn.account}")
    return web.json_response({"ok": True, "account": conn.account})


async def api_secret_backend_disconnect(request: web.Request) -> web.Response:
    """DELETE /api/secrets/backends/{id} — forget the token. Nothing at the provider changes."""
    operation = "secrets_backend_disconnect"
    gated = await _gate(request, operation)
    if gated is not None:
        return gated
    backend = _backend_or_404(request)
    if isinstance(backend, web.Response):
        return backend
    vault = SecretVault(secrets_handlers.config_dir())
    try:
        conn = await asyncio.to_thread(secret_backends.read_connection, vault, backend)
    except secret_backends.BackendError:
        # An unreadable connection has no token to hand the provider; forget it.
        conn = None
    if conn is not None:
        try:
            await asyncio.to_thread(
                secret_backends.call, backend, lambda: backend.disconnect(conn.data)
            )
        except secret_backends.BackendError:
            # The provider side is best effort; forgetting the token is not.
            logger.info("%s disconnect hook failed; forgetting the token anyway", backend.id)
    await vault.delete(secret_backends.connection_item(backend.id))
    _audit(request, operation, "ok", backend.id)
    return web.json_response({"ok": True})


def setup_secret_backend_routes(app: web.Application) -> None:
    app.router.add_post("/api/secrets/backends/{backend_id}", api_secret_backend_connect)
    app.router.add_delete("/api/secrets/backends/{backend_id}", api_secret_backend_disconnect)
