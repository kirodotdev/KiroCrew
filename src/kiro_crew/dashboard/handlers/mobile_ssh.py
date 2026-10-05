"""Loopback-only API for mobile SSH enrollment and token minting."""

from __future__ import annotations

import asyncio
import hmac
import os
import shutil
import sys
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.mobile_connect import mint_denied_reason
from kiro_crew.dashboard.origin import is_loopback
from kiro_crew.member_memory_auth import local_owner_bootstrap_allowed
from kiro_crew.mobile_ssh import (
    MOBILE_BRIDGE_COMMAND,
    MOBILE_ENROLLMENT_SCHEMA,
    MOBILE_ERROR_SCHEMA,
    MOBILE_REVOCATION_SCHEMA,
    MOBILE_SSH_SCHEMA,
    MobileSshError,
    discover_host_ssh_identity,
    get_mobile_ssh_store,
)
from kiro_crew.sel import sel

_MAX_REQUEST_BYTES = 4096


def _error_response(code: str, message: str, status: int) -> web.Response:
    return web.json_response(
        {"schema": MOBILE_ERROR_SCHEMA, "ok": False, "code": code, "error": message},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


async def _read_body(request: web.Request) -> dict[str, Any]:
    data, error = await read_bounded_json(request, max_bytes=_MAX_REQUEST_BYTES)
    if error is not None:
        raise _ResponseError(error)
    if data is None:
        raise MobileSshError("invalid_json", "request body must be a JSON object")
    return data


class _ResponseError(Exception):
    def __init__(self, response: web.Response) -> None:
        super().__init__("request refused")
        self.response = response


async def _host_process_error(request: web.Request) -> web.Response | None:
    if not is_loopback(request.remote or ""):
        return _error_response("local_only", "mobile SSH routes are loopback-only", 403)
    # Same host-provenance gate as /api/token/local: refuses a sandboxed agent process.
    if not await asyncio.to_thread(local_owner_bootstrap_allowed, request):
        return _error_response(
            "member_owner_token_refused", "mobile SSH routes require a host-owned process", 403
        )
    return None


async def _owner_error(request: web.Request) -> web.Response | None:
    expected = str(request.app.get("local_secret", ""))
    provided = request.headers.get("X-Local-Secret", "")
    if not is_loopback(request.remote or ""):
        return _error_response("local_only", "mobile SSH administration is loopback-only", 403)
    # Bytes compare: a non-ASCII header must be a 403, not a TypeError from compare_digest.
    if (
        not expected
        or not provided
        or not hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))
    ):
        return _error_response("local_auth_failed", "local gateway authentication failed", 403)
    return await _host_process_error(request)


def _executable(candidate: str) -> str:
    # A module path (`python -m`) is never a launcher; Windows reports X_OK for any file.
    if not candidate or os.path.splitext(candidate)[1].lower() in (".py", ".pyc", ".pyw"):
        return ""
    path = os.path.abspath(candidate)
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else ""


def _launcher() -> str:
    # The forced command must run THIS installation. Prefer the unresolved PATH shim so the
    # line survives versioned upgrades, but only when it resolves to the running launcher.
    running = _executable(sys.argv[0]) or _executable(shutil.which(sys.argv[0]) or "")
    if not running:
        running = _executable(shutil.which("kirocrew") or "")
    if not running:
        raise MobileSshError(
            "launcher_unavailable", "the running kirocrew launcher is not executable", status=503
        )
    shim = _executable(shutil.which("kirocrew") or "")
    if shim and os.path.realpath(shim) == os.path.realpath(running):
        return shim
    return running


async def _require_mobile_connect() -> None:
    # The mobile_connect seam governs every credential a phone can obtain.
    denied = await asyncio.to_thread(mint_denied_reason, "ssh_device")
    if denied:
        raise MobileSshError("mobile_connect_denied", denied, status=403)


def _served_port(request: web.Request) -> int:
    # The loopback socket this request arrived on is the port the bridge must dial.
    transport = request.transport
    sockname = transport.get_extra_info("sockname") if transport is not None else None
    port = sockname[1] if isinstance(sockname, tuple) and len(sockname) >= 2 else None
    if not isinstance(port, int) or not 1 <= port <= 65535:
        port = request.app.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise MobileSshError(
            "gateway_port_unknown", "could not determine the gateway port", status=503
        )
    return port


def _audit(operation: str, outcome: str, error: str = "") -> None:
    sel().log_api_access(
        caller="local",
        operation=operation,
        outcome=outcome,
        source="token_auth",
        resources="mobile_ssh",
        error=error,
    )


async def _run(request: web.Request, operation: str, gate: Any, fn: Any) -> web.Response:
    denied = await gate(request)
    if denied is not None:
        _audit(operation, "denied", "caller refused")
        return denied
    try:
        result = await fn()
    except _ResponseError as exc:
        _audit(operation, "failed", "invalid request body")
        exc.response.headers["Cache-Control"] = "no-store"
        return exc.response
    except MobileSshError as exc:
        _audit(operation, "denied" if exc.status in (401, 403) else "failed", exc.code)
        return _error_response(exc.code, exc.message, exc.status)
    _audit(operation, "ok")
    return web.json_response(result, headers={"Cache-Control": "no-store"})


async def api_mobile_ssh_enroll(request: web.Request) -> web.Response:
    async def action() -> dict[str, object]:
        data = await _read_body(request)
        await _require_mobile_connect()
        gateway_port = _served_port(request)
        host_identity = await asyncio.to_thread(discover_host_ssh_identity)
        store = await asyncio.to_thread(get_mobile_ssh_store)
        launcher = await asyncio.to_thread(_launcher)
        result = await asyncio.to_thread(
            store.enroll,
            str(data.get("device_id", "")),
            str(data.get("public_key", "")),
            label=str(data.get("label", "")),
            launcher=launcher,
            gateway_port=gateway_port,
        )
        return {
            "schema": MOBILE_ENROLLMENT_SCHEMA,
            "ok": True,
            "device": result.device.public_metadata(),
            "authorized_keys_line": result.authorized_keys_line,
            "ssh_command": MOBILE_BRIDGE_COMMAND,
            "ssh_username": host_identity.username,
            "host_key_algorithm": host_identity.host_key_algorithm,
            "host_key_fingerprint": host_identity.host_key_fingerprint,
        }

    return await _run(request, "mobile_ssh.enroll", _owner_error, action)


async def api_mobile_ssh_devices(request: web.Request) -> web.Response:
    async def action() -> dict[str, object]:
        store = await asyncio.to_thread(get_mobile_ssh_store)
        devices = await asyncio.to_thread(store.list_devices)
        return {"schema": MOBILE_SSH_SCHEMA, "ok": True, "devices": devices}

    return await _run(request, "mobile_ssh.list", _owner_error, action)


async def api_mobile_ssh_revoke(request: web.Request) -> web.Response:
    async def action() -> dict[str, object]:
        data = await _read_body(request)
        store = await asyncio.to_thread(get_mobile_ssh_store)
        device = await asyncio.to_thread(store.revoke, str(data.get("device_id", "")))
        return {"schema": MOBILE_REVOCATION_SCHEMA, "ok": True, "device": device.public_metadata()}

    return await _run(request, "mobile_ssh.revoke", _owner_error, action)


async def api_mobile_ssh_challenge(request: web.Request) -> web.Response:
    async def action() -> dict[str, object]:
        data = await _read_body(request)
        await _require_mobile_connect()
        store = await asyncio.to_thread(get_mobile_ssh_store)
        return await asyncio.to_thread(store.issue_challenge, str(data.get("device_id", "")))

    return await _run(request, "mobile_ssh.challenge", _host_process_error, action)


async def api_mobile_ssh_token(request: web.Request) -> web.Response:
    async def action() -> dict[str, object]:
        data = await _read_body(request)
        await _require_mobile_connect()
        store = await asyncio.to_thread(get_mobile_ssh_store)
        return await asyncio.to_thread(
            store.mint,
            str(data.get("device_id", "")),
            str(data.get("nonce", "")),
            str(data.get("signature", "")),
        )

    return await _run(request, "mobile_ssh.token", _host_process_error, action)
