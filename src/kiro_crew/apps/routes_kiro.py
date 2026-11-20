"""``POST /api/apps/{name}/kiro/{operation}``: an app's call to an approved Kiro operation.

A JSON object in, the operation's output map out as JSON (``apps/kiro_calls.py``).
Two callers are admitted, each named positively:

* **the app's backend**: an app token whose verified claim is exactly ``{name}``,
  authenticated as a token (``request["auth_token"]``) and not through the internal
  secret, which a process in an agent sandbox can read;
* **the host owner**: the machine owner's own dashboard session
  (``is_host_owner_dashboard_request``). A Shared Crew teammate is not admitted:
  the call would run as the owner's Kiro sign-in.

Every other caller gets 403 ``kiro_caller_refused`` and nothing is sent. The path
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
    call_kiro_operation,
    parse_json_body,
)


def kiro_caller(request: web.Request, name: str) -> str | None:
    """``"backend"`` or ``"owner"`` when *request* may call *name*'s operations; else None."""
    if request.get("internal_auth") is True:
        return None
    if request.get("app") == name and bool(request.get("auth_token")):
        return "backend"
    from kiro_crew.dashboard.handlers.source_providers import (  # noqa: PLC0415
        is_host_owner_dashboard_request,
    )

    return "owner" if is_host_owner_dashboard_request(request) else None


def _kiro_error(exc: KiroCallError) -> web.Response:
    return web.json_response(exc.body(), status=exc.status)


async def handle_kiro_call(request: web.Request) -> web.Response:
    """Forward one approved operation for the app the path names."""
    name = request.match_info["name"]
    operation = request.match_info["operation"]
    caller = kiro_caller(request, name)
    if caller is None:
        return _kiro_error(
            KiroCallError(
                403,
                "kiro_caller_refused",
                "Only this app's own token or the owner may call its Kiro operations.",
            )
        )
    if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
        return _kiro_error(KiroCallError(413, "body_too_large", "The input is too large."))
    try:
        payload = parse_json_body(await request.content.read(MAX_REQUEST_BYTES + 1))
        output = await call_kiro_operation(name, operation, payload, caller=caller)
    except KiroCallError as exc:
        return _kiro_error(exc)
    return web.json_response(output)


def register_kiro_call_routes(app: web.Application) -> None:
    """Mount the route; called before the app route registry's catch-all."""
    app.router.add_post("/api/apps/{name}/kiro/{operation}", handle_kiro_call)


__all__ = ["handle_kiro_call", "kiro_caller", "register_kiro_call_routes"]
