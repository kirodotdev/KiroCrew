"""``POST /api/apps/{name}/_sel``: the gateway writes an app backend's audit rows.

A spawned app backend must not hold the SEL MAC key (``trust/sel_hmac.key``), so
it asks the gateway to sign and append. The caller proves who it is with the
app-scoped token from ``POST /api/apps/{name}/token`` (the App Kit exchange every
backend already has the secret for); the middleware stamps ``request["app"]`` and
the route adds no perimeter exemption.

What the body can and cannot say:

* The actor is ``app:<request["app"]>``, set here. A body that names ``caller``,
  ``caller_identity``, ``source``, ``session_key``, ``agent`` or any field outside
  the allowlist is a 400, so there is no way to name another actor.
* One event kind only: ``api_access`` (``sel.py`` ``log_api_access``). The gateway's
  security kinds -- ``governance_decision`` (``sel.py:893``), ``governance_degraded``
  (``sel.py:945``), ``tool_invocation`` (``sel.py:820``, approvals and credential
  use) and ``sel_rotation`` (``sel.py:698``) -- are refused.
* The operation is kept under the app's own namespace (``<app>.<operation>``), so
  an app cannot reuse a gateway operation name such as ``internal_auth``.
* ``source`` is ``app``, so a reader shows the row as app-sourced.

Responses never echo the body. A refused write is a status code and a fixed string.

shortcut: the limiter is per process and per app, in memory; upgrade to a shared
budget if the gateway ever runs more than one process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from aiohttp import web

from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Largest request body accepted, in bytes. ``log_api_access`` clips ``outcome``,
#: ``resources`` and ``error`` to 500 characters (``sel.py:111``) but stores
#: ``operation`` as given, so ``operation`` is capped here instead.
_MAX_BODY = 16 * 1024
#: Longest ``operation`` accepted, before the ``<app>.`` prefix (an app name is at most
#: 64 characters, so the stored value stays under the shared 500-character clip).
_MAX_OPERATION = 200
#: Sustained rows per second and burst, per app. A refused call never reaches the
#: log, so the limiter cannot grow or rotate the segment.
_RATE = 50.0
_BURST = 200
_DENY_LOG_EVERY = 60.0

#: method -> (required kwargs, optional kwargs). Everything else is a 400.
_ALLOWED: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "log_api_access": (
        frozenset({"operation", "outcome"}),
        frozenset({"resources", "error", "critical"}),
    ),
}

#: app -> [tokens, last refill, last denial row]. Touched only on the event loop.
_buckets: dict[str, list[float]] = {}


def _now() -> float:
    return time.monotonic()


def _take(app: Any, buckets: dict[Any, list[float]] | None = None) -> tuple[bool, bool]:
    """``(allowed, log_denial)`` for one write by *app*, in *buckets* (this route's own
    by default; ``dashboard.handlers.ipc_sel`` passes its own, keyed by session and
    server)."""
    now = _now()
    b = (_buckets if buckets is None else buckets).setdefault(
        app, [float(_BURST), now, -_DENY_LOG_EVERY]
    )
    b[0] = min(float(_BURST), b[0] + (now - b[1]) * _RATE)
    b[1] = now
    if b[0] >= 1.0:
        b[0] -= 1.0
        return True, False
    log = now - b[2] >= _DENY_LOG_EVERY
    if log:
        b[2] = now
    return False, log


def _parse(raw: bytes) -> tuple[str, dict[str, Any]] | None:
    """``(method, kwargs)`` when the body is exactly the allowed shape, else None."""
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):  # RecursionError: a deeply nested body under the cap
        return None
    if not isinstance(body, dict) or set(body) != {"method", "kwargs"}:
        return None
    method, kwargs = body["method"], body["kwargs"]
    if not isinstance(method, str) or method not in _ALLOWED or not isinstance(kwargs, dict):
        return None
    required, optional = _ALLOWED[method]
    if not required <= set(kwargs) or not set(kwargs) <= required | optional:
        return None
    for key, value in kwargs.items():
        if not isinstance(value, bool if key == "critical" else str):
            return None
    if len(kwargs["operation"]) > _MAX_OPERATION:
        return None
    return method, kwargs


def _write(app: str, method: str, kwargs: dict[str, Any]) -> None:
    operation = str(kwargs["operation"])
    if not operation.startswith(f"{app}."):
        operation = f"{app}.{operation}"
    getattr(sel(), method)(caller=f"app:{app}", source="app", **{**kwargs, "operation": operation})


def _refuse(error: str, code: str, status: int) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=status)


async def _read_capped(request: web.Request) -> bytes | None:
    """The whole body, or None once it passes the cap.

    ``StreamReader.read(n)`` returns as soon as any data is buffered, so a body that
    arrives in several chunks would be cut short; read to EOF and stop at the cap.
    """
    raw = bytearray()
    async for chunk in request.content.iter_chunked(8192):
        raw += chunk
        if len(raw) > _MAX_BODY:
            return None
    return bytes(raw)


async def handle_sel(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    app = request.get("app", "")
    if not app or app != name:
        return _refuse("app token required", "app_token_required", 403)
    allowed, log_denial = _take(app)
    if not allowed:
        if log_denial:
            await asyncio.to_thread(_deny_row, app)
        return _refuse("rate limited", "sel_rate_limited", 429)
    raw = await _read_capped(request)
    if raw is None:
        return _refuse("body too large", "sel_body_too_large", 413)
    parsed = _parse(raw)
    if parsed is None:
        return _refuse("invalid body", "sel_invalid_body", 400)
    try:
        await asyncio.to_thread(_write, app, *parsed)
    except Exception:  # noqa: BLE001 - one fixed answer; the cause stays in the gateway log
        logger.warning("app %s: SEL write failed", app, exc_info=True)
        return _refuse("audit write failed", "sel_write_failed", 500)
    return web.Response(status=204)


def _deny_row(app: str) -> None:
    try:
        sel().log_api_access(
            caller=f"app:{app}",
            operation="sel_route",
            outcome="denied",
            source="app_auth",
            error="rate limited",
        )
    except Exception:  # noqa: BLE001 - best effort; the 429 already answered
        logger.debug("SEL rate-limit row failed", exc_info=True)


def register_sel_routes(app: web.Application) -> None:
    """Mount ``_sel`` for every app. Call before the Route Registry catch-all."""
    app.router.add_post("/api/apps/{name}/_sel", handle_sel)
