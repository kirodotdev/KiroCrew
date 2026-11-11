"""Connect and disconnect routes for third-party secret backends.

One set of routes serves every backend in :mod:`kiro_crew.secrets.backend`'s
registry: ``POST /api/secrets/backends/{id}`` with one connect form's values
(``"form": "main"`` or ``"fallback"``), ``POST .../{id}/approve`` to approve
again in the provider's app, ``DELETE /api/secrets/backends/{id}`` to forget
the connection. No route, and no branch here, is specific to one provider. The
listing half lives in ``GET /api/secrets`` (``secrets.api_secrets_list``).
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.config.paths import data_home
from kiro_crew.dashboard.handlers import secrets as secrets_handlers
from kiro_crew.secrets import SecretVault
from kiro_crew.secrets import backend as secret_backends
from kiro_crew.secrets import items

logger = logging.getLogger(__name__)

#: Per backend, bumped by every Disconnect and every saved connection. A connect
#: or Approve again that started before the bump does not save: a desktop-app
#: approval can land long after the person clicked Disconnect or connected
#: another way, and must not bring back or replace what they chose since.
# shortcut: per gateway process, which is where every route runs; move it into
# the vault entry's own revision if the routes ever run in more than one process.
_GENERATION: dict[str, int] = {}
_LOCKS: dict[str, asyncio.Lock] = {}
#: What the last bump was, so a stale save says why it did not save.
_LAST_CHANGE: dict[str, str] = {}


def reset_for_tests() -> None:
    """Forget every generation, last change and lock (one gateway owns them)."""
    _GENERATION.clear()
    _LAST_CHANGE.clear()
    _LOCKS.clear()


def _lock(backend_id: str) -> asyncio.Lock:
    """Held only around the generation check and the vault write, never the provider call."""
    return _LOCKS.setdefault(backend_id, asyncio.Lock())


async def _gate(request: web.Request, operation: str) -> web.Response | None:
    """The owner gate."""
    return await secrets_handlers._owner_only(request, operation)


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
    started = _GENERATION.get(backend.id, 0)
    vault = SecretVault(data_home())
    try:
        values = secret_backends.form_values(backend, body)
    except secret_backends.BackendError as exc:
        return await _failed(request, operation, backend, exc)
    return await _connect_and_store(request, operation, backend, vault, values, started)


async def _failed(
    request: web.Request,
    operation: str,
    backend: secret_backends.SecretBackend,
    exc: secret_backends.BackendError,
) -> web.Response:
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


async def _connect_and_store(
    request: web.Request,
    operation: str,
    backend: secret_backends.SecretBackend,
    vault: SecretVault,
    values: dict[str, str],
    started: int,
) -> web.Response:
    """Check *values* against the provider (a person may approve in its app
    first), then store the connection in the local vault, unless a Disconnect
    ran since *started* (the generation the route read before it read anything)."""
    try:
        conn = await asyncio.to_thread(
            secret_backends.call,
            backend,
            lambda: backend.connect(values),
            secret_backends.CONNECT_TIMEOUT_SECS,
        )
    except secret_backends.BackendError as exc:
        return await _failed(request, operation, backend, exc)
    try:
        async with _lock(backend.id):
            if _GENERATION.get(backend.id, 0) != started:
                if _LAST_CHANGE.get(backend.id) == "replaced":
                    stale = secret_backends.BackendError(
                        "replaced", f"{backend.display_name} was connected another way meanwhile."
                    )
                else:
                    stale = secret_backends.BackendError(
                        "disconnected", f"{backend.display_name} was disconnected meanwhile."
                    )
                return await _failed(request, operation, backend, stale)
            await vault.set_item_with_revision(
                secret_backends.connection_item(backend.id),
                items.ITEM_TYPE_NOTE,
                secret_backends.connection_fields(conn),
            )
            _GENERATION[backend.id] = _GENERATION.get(backend.id, 0) + 1
            _LAST_CHANGE[backend.id] = "replaced"
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


async def api_secret_backend_approve(request: web.Request) -> web.Response:
    """POST /api/secrets/backends/{id}/approve — Approve again.

    Connects once more with the stored connection's own main-form values (an
    account name, never a token), so the provider's app asks the person again.
    """
    operation = "secrets_backend_approve"
    gated = await _gate(request, operation)
    if gated is not None:
        return gated
    backend = _backend_or_404(request)
    if isinstance(backend, web.Response):
        return backend
    # Read before the stored connection: a Disconnect between the two must win.
    started = _GENERATION.get(backend.id, 0)
    vault = SecretVault(data_home())
    try:
        conn = await asyncio.to_thread(secret_backends.read_connection, vault, backend)
        if conn is None:
            raise secret_backends.BackendError("invalid_input", "Connect first.")
        values = secret_backends.approve_values(backend, conn)
    except secret_backends.BackendError as exc:
        return await _failed(request, operation, backend, exc)
    return await _connect_and_store(request, operation, backend, vault, values, started)


async def api_secret_backend_disconnect(request: web.Request) -> web.Response:
    """DELETE /api/secrets/backends/{id} — forget the token. Nothing at the provider changes."""
    operation = "secrets_backend_disconnect"
    gated = await _gate(request, operation)
    if gated is not None:
        return gated
    backend = _backend_or_404(request)
    if isinstance(backend, web.Response):
        return backend
    vault = SecretVault(data_home())
    try:
        conn = await asyncio.to_thread(secret_backends.read_connection, vault, backend)
    except secret_backends.BackendError:
        # An unreadable connection has no token to hand the provider; forget it.
        # Deleting the name decrypts nothing, so a damaged entry still goes.
        conn = None
    try:
        async with _lock(backend.id):
            # Stored first: the generation moves only once the token is gone.
            await vault.delete(secret_backends.connection_item(backend.id))
            _GENERATION[backend.id] = _GENERATION.get(backend.id, 0) + 1
            _LAST_CHANGE[backend.id] = "disconnected"
    except (OSError, items.ItemError):
        logger.warning("%s connection not removed: the vault write failed", backend.id)
        _audit(request, operation, "failed", f"{backend.id}:vault_write_failed")
        return web.json_response(
            {
                "error": "Could not remove the connection from the vault. Check disk "
                "space and permissions, then try again.",
                "code": "vault_write_failed",
            },
            status=500,
        )
    if conn is not None:
        try:
            await asyncio.to_thread(
                secret_backends.call, backend, lambda: backend.disconnect(conn.data)
            )
        except secret_backends.BackendError:
            # The provider side is best effort; the token is already forgotten.
            logger.info("%s disconnect hook failed; the token is forgotten anyway", backend.id)
    _audit(request, operation, "ok", backend.id)
    return web.json_response({"ok": True})


def setup_secret_backend_routes(app: web.Application) -> None:
    app.router.add_post("/api/secrets/backends/{backend_id}", api_secret_backend_connect)
    app.router.add_post("/api/secrets/backends/{backend_id}/approve", api_secret_backend_approve)
    app.router.add_delete("/api/secrets/backends/{backend_id}", api_secret_backend_disconnect)
