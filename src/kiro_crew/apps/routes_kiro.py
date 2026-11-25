"""``POST /api/apps/{name}/kiro/{operation}``: an app's call to an approved Kiro operation.

A JSON object in, the operation's output map out as JSON (``apps/kiro_calls.py``).
Two callers are admitted, each named positively:

* **the app's backend**: an app token whose verified claim is exactly ``{name}``,
  authenticated as a token (``request["auth_token"]``) and not through the internal
  secret, which a process in an agent sandbox can read;
* **the host owner**: the machine owner's own dashboard session
  (``is_host_owner_dashboard_request``). A Shared Crew teammate is not admitted:
  the call would run as the owner's Kiro sign-in.

Every other caller gets 403 ``forbidden`` and nothing is sent. The path
sits in the app's own namespace (``token_auth_app_scope._app_owns_path``), so the
app declares no ``permissions.api`` entry for it; ``kiro`` is a core segment of
that namespace (``manifest.CORE_APP_ROUTE_SEGMENTS``). An ``agentTools`` handler
reaches the seam the same way, with the app's token or through ``ctx.kiro_call``.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.apps.kiro_calls import (
    MAX_REQUEST_BYTES,
    KiroCallError,
    audit_kiro_call,
    call_kiro_operation,
    parse_json_body,
)
from kiro_crew.apps.kiro_operations import MAX_KIRO_OPERATIONS

_KIRO_READ_CHUNK = 64 * 1024


def kiro_caller(request: web.Request, name: str) -> str | None:
    """``"backend"`` or ``"owner"`` when *request* may call *name*'s operations; else None."""
    if request.get("internal_auth") is True:
        return None
    if request.get("app") == name and bool(request.get("auth_token")):
        return "backend"
    return "owner" if _is_host_owner(request) else None


def _is_host_owner(request: web.Request) -> bool:
    from kiro_crew.dashboard.handlers.source_providers import (  # noqa: PLC0415
        is_host_owner_dashboard_request,
    )

    return is_host_owner_dashboard_request(request)


def kiro_consent_refusal(
    request: web.Request, grants_consent: dict[str, list[str]] | None
) -> web.Response | None:
    """The refusal when anyone but the host owner approves Kiro operations; else None.

    The calls run with the host owner's Kiro sign-in, so a Shared Crew teammate, who
    passes the dashboard's owner gate, may not choose them (``POST .../enable``).
    """
    if not grants_consent or not grants_consent.get("kiroOperations"):
        return None
    if _is_host_owner(request):
        return None
    app = request.match_info.get("name", "")
    for operation in grants_consent["kiroOperations"][:MAX_KIRO_OPERATIONS]:
        audit_kiro_call(app, operation, "approval", "denied", "not the host owner")
    return web.json_response(
        {
            "error": "Only the owner can approve Kiro operations: they run with the owner's Kiro sign-in.",
            "code": "owner_only",
        },
        status=403,
    )


def _kiro_error(exc: KiroCallError) -> web.Response:
    return web.json_response(
        {"error": exc.message, "code": exc.code, "upstream": exc.upstream},
        status=exc.status,
        headers={"Retry-After": "1"} if exc.status == 429 else None,
    )


async def _read_kiro_body(request: web.Request) -> bytes:
    """The whole body, read until it ends; past :data:`MAX_REQUEST_BYTES` it is refused."""
    raw = bytearray()
    while chunk := await request.content.read(_KIRO_READ_CHUNK):
        raw += chunk
        if len(raw) > MAX_REQUEST_BYTES:
            raise KiroCallError(413, "body_too_large", "The input is too large.")
    return bytes(raw)


async def handle_kiro_call(request: web.Request) -> web.Response:
    """Forward one approved operation for the app the path names."""
    name = request.match_info["name"]
    operation = request.match_info["operation"]
    caller = kiro_caller(request, name)
    if caller is None:
        audit_kiro_call(name, operation, "refused", "denied", "forbidden")
        return _kiro_error(
            KiroCallError(
                403,
                "forbidden",
                "Only this app's own token or the owner may call its Kiro operations.",
            )
        )
    if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
        return _kiro_error(KiroCallError(413, "body_too_large", "The input is too large."))
    try:
        payload = parse_json_body(await _read_kiro_body(request))
        output = await call_kiro_operation(name, operation, payload, caller=caller)
    except KiroCallError as exc:
        return _kiro_error(exc)
    return web.json_response(output)


def register_kiro_call_routes(app: web.Application) -> None:
    """Mount the route; called before the app route registry's catch-all."""
    app.router.add_post("/api/apps/{name}/kiro/{operation}", handle_kiro_call)


__all__ = ["handle_kiro_call", "kiro_caller", "kiro_consent_refusal", "register_kiro_call_routes"]
