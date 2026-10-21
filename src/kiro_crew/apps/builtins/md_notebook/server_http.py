"""Request plumbing for the Notes backend: the error type, bodies, auth and errors.

``ApiError`` is the handler failure that carries an HTTP status; ``json_body``,
``require_vault`` and ``require_writable`` read and check what a request names;
``proxy_auth_middleware`` verifies the gateway's HMAC signature on every request but the
liveness probe; and ``error_middleware`` turns a failure into the JSON error body,
scrubbed by ``_safe_error`` before it reaches the browser. Reached through ``server``,
its facade.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from aiohttp import web

from kiro_crew import security
from kiro_crew.apps.builtins.md_notebook import git_ops
from kiro_crew.apps.builtins.md_notebook.server_store import APP_NAME, logger, read_vaults
from kiro_crew.apps.proxy_auth import raw_request_target, verify_proxy_request
from kiro_crew.sel import sel


class ApiError(Exception):
    """A handler failure carrying an HTTP status."""

    def __init__(
        self, message: str, status: int = 500, code: str = "request_failed", **extra: Any
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra


#: Named for ``server``, the module callers import the class from, so a traceback or a
#: pickle names ``server.ApiError``, as the imported module did when it defined the class.
ApiError.__module__ = "kiro_crew.apps.builtins.md_notebook.server"


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------


async def json_body(request: web.Request) -> dict[str, Any]:
    """Parse the JSON body.

    Uses ``request.read()``, which returns aiohttp's cached copy — the auth
    middleware has already consumed the stream to verify the signature, so
    reading the raw stream again would yield nothing. The size ceiling is
    enforced by the Application's ``client_max_size``.
    """
    raw = await request.read()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(f"invalid JSON body: {exc.msg}", 400, code="invalid_json_body") from exc
    return data if isinstance(data, dict) else {}


async def require_vault(request: web.Request) -> dict[str, Any]:
    """The requested vault, or the first one when no id is given."""
    vaults = await read_vaults()
    wanted = request.query.get("vault") or (vaults[0]["id"] if vaults else None)
    for v in vaults:
        if v["id"] == wanted:
            return v
    raise ApiError("no vault connected", 404, code="no_vault_connected")


def require_writable(vault: dict[str, Any]) -> None:
    if vault.get("readOnly"):
        raise ApiError("vault is read-only", 403, code="vault_read_only")


@web.middleware
async def proxy_auth_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
    """Verify the gateway's HMAC on every request except the liveness probe.

    Fails closed: the bare ``/health`` path is the gateway's own in-process
    poll and is the only exemption. Everything else must carry a fresh
    signature, so a stray local process cannot drive this backend.
    """
    if request.path == "/health":
        return await handler(request)
    # Verify over the RAW request-target — the exact bytes the gateway signed;
    # the decoded path + query_string diverge on percent-encodable characters.
    target = raw_request_target(request)
    body = await request.read() if request.can_read_body else b""
    if not verify_proxy_request(
        request.headers.get("X-KiroCrew-Proxy", ""),
        method=request.method,
        target=target,
        body=body,
    ):
        # Audit the denial before answering, following the file-explorer
        # builtin's proxy_auth_failed convention — an unsigned local process
        # probing this backend is exactly what the SEL trail exists to record.
        # SEL writes are blocking file I/O, so emit off-loop; log only the
        # path (no query string), which can carry note names.
        def _audit_denied() -> None:
            sel().log_api_access(
                caller=APP_NAME,
                operation="proxy_auth_failed",
                outcome="denied",
                source="builtin-app",
                resources=request.path,
            )

        await asyncio.to_thread(_audit_denied)
        return web.json_response(
            {"error": "invalid or missing proxy signature", "code": "invalid_proxy_signature"},
            status=401,
        )
    return await handler(request)


def _error_status(exc: BaseException) -> int:
    if isinstance(exc, ApiError):
        return exc.status
    if isinstance(exc, git_ops.AttachError):
        return 400
    return 500


def _error_code(exc: BaseException) -> str:
    """A stable machine-readable id, so the UI can localize its own copy."""
    if isinstance(exc, ApiError):
        return exc.code
    if isinstance(exc, git_ops.AttachError):
        return exc.code
    if isinstance(exc, git_ops.GitError):
        return "git_failed"
    return "io_failed"


#: `error` is advisory English prose; `code` is the contract the dashboard keys
#: its localized message off. aiohttp's exception classes carry the status, so
#: each one is a literal rather than a computed `status=` argument.
_HTTP_ERRORS: dict[int, type[web.HTTPException]] = {
    400: web.HTTPBadRequest,
    401: web.HTTPUnauthorized,
    403: web.HTTPForbidden,
    404: web.HTTPNotFound,
    409: web.HTTPConflict,
    413: web.HTTPRequestEntityTooLarge,
    500: web.HTTPInternalServerError,
    501: web.HTTPNotImplemented,
}


def _safe_error(exc: BaseException) -> str:
    """Exception text with credentials, exfiltration URLs and host paths stripped.

    The backend's handlers drive git and filesystem work over caller-supplied vault URLs,
    ids and note paths, so a raw message routinely carries absolute paths and git
    stderr. Both branches of ``error_middleware`` send their text to the browser.

    The path pass is the load-bearing one: the dominant shape here is an OS error
    like ``[Errno 2] No such file or directory: '/home/<user>/.kiro/crew/...'``,
    which the credential and URL passes do not match at all.
    """
    text = security.redact(str(exc))
    text, _ = security.redact_local_paths(text)
    return text


@web.middleware
async def error_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
    """Turn handler exceptions into the JSON error shape the UI expects."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (ApiError, git_ops.AttachError, git_ops.GitError, OSError) as exc:
        payload: dict[str, Any] = {"error": _safe_error(exc), "code": _error_code(exc)}
        if isinstance(exc, ApiError):
            payload.update(exc.extra)
        cls = _HTTP_ERRORS.get(_error_status(exc), web.HTTPInternalServerError)
        raise cls(text=json.dumps(payload), content_type="application/json") from exc
    except Exception as exc:  # noqa: BLE001 — never leak a traceback to the UI
        logger.exception("md-notebook: unhandled error on %s", request.path)
        return web.json_response({"error": _safe_error(exc), "code": "internal_error"}, status=500)
