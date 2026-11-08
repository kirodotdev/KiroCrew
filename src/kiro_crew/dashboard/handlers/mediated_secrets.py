"""The two host-side routes behind the ``kirocrew-secrets`` MCP tool.

``POST /api/mediated-secret-capability`` redeems the single-use grant the ACP
approval minted for one ``call_api_with_secret`` call, and
``POST /api/mediated-secret-request`` performs the owner-authorized outbound
HTTPS request with the Custom secret injected HERE, in the unsandboxed gateway,
so the value never reaches the agent.

Both are MCP-only machine routes (``_STRICT_INTERNAL_API_PATHS``): the caller
must hold the internal secret AND an attested member-session scope
(``member_request_scope``). Bound to the route table through
``mcp_routes._deferred`` so the gateway boot path never imports this module, and
the mediation stack (``requests``, the vault) is imported inside the worker
thread, never on the event loop.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from aiohttp import web

from kiro_crew import mediated_request_capability
from kiro_crew.config.loader import config_dir as _config_dir
from kiro_crew.dashboard.handlers._shared import member_request_scope, read_bounded_json


def _sel():
    """Late-binding ``sel()``, the same circular-import exception every sibling uses."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811 -- circular import

    return _pkg.sel()


def _audit(operation: str, caller: str, outcome: str, resources: str) -> None:
    _sel().log_api_access(
        caller=caller,
        operation=operation,
        outcome=outcome,
        source="dashboard",
        resources=resources,
    )


async def _admit(
    request: web.Request, operation: str
) -> tuple[str, dict[str, Any] | None, web.Response | None]:
    """The gates both routes share, in order: internal authority, a session key,
    an attested member scope for THAT key, then a bounded JSON object body.

    Returns ``(session_key, body, None)`` or ``("", None, refusal)``. The body is
    read to EOF under the mediated-request cap (``read_bounded_json`` drains the
    stream chunk by chunk), so an allowed payload larger than one transport read
    is never decoded as a truncated prefix.
    """
    # Machines only. A credential-bearing egress must never be reachable through
    # the browser cookie fall-through: the proven internal-secret authority comes
    # first, so a dashboard bearer that slipped past strict-path routing still
    # cannot drive a request that skips the MCP approval gate.
    if request.get("internal_auth") is not True:
        _audit(
            operation,
            request.headers.get("X-Session-Key", "unknown"),
            "denied",
            "not an internal-secret caller",
        )
        return (
            "",
            None,
            web.json_response(
                {"error": "internal authority required", "code": "internal_auth_required"},
                status=403,
            ),
        )
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        _audit(operation, "unknown", "denied", "missing X-Session-Key")
        return (
            "",
            None,
            web.json_response(
                {"error": "X-Session-Key required", "code": "session_key_required"}, status=400
            ),
        )
    # Bound to an ATTESTED member session, not merely "a same-host process holding
    # the loopback secret": ``member_request_scope`` honours X-Session-Key only
    # behind a transport attestation (``session_key_is_attested``) and reads the
    # canonical execution record before trusting it. A raw in-sandbox program that
    # read the loopback secret and a session key off a transcript has neither, so
    # its scope is unverified and it is refused here.
    scope = await member_request_scope(request)
    if not scope.verified or scope.session != session_key or not scope.store:
        _audit(operation, session_key, "denied", "unverified or non-member session scope")
        return (
            "",
            None,
            web.json_response(
                {
                    "error": (
                        "This request must come from an approved kirocrew-secrets tool call "
                        "in a member session; the session identity is unverified or is not "
                        "a member session."
                    ),
                    "code": "member_scope_required",
                },
                status=403,
            ),
        )
    body, error = await read_bounded_json(
        request, max_bytes=mediated_request_capability.MAX_REQUEST_PAYLOAD_BYTES
    )
    if error is not None:
        _audit(operation, session_key, "denied", "request body refused before decoding")
        return "", None, error
    return session_key, body, None


async def api_mediated_secret_capability(request: web.Request) -> web.Response:
    """Claim the single-use capability minted for one approved MCP invocation."""
    operation = "mediated_secret_capability"
    session_key, payload, refusal = await _admit(request, operation)
    if refusal is not None:
        return refusal
    assert payload is not None
    tool_call_id = payload.get("tool_call_id")
    intent = payload.get("request")
    if not isinstance(tool_call_id, str) or not isinstance(intent, dict):
        _audit(operation, session_key, "denied", "approved call identity required")
        return web.json_response(
            {"error": "approved call identity required", "code": "approved_call_required"},
            status=403,
        )
    capability = mediated_request_capability.claim(session_key, tool_call_id, intent)
    if not capability:
        # Audited by tool-call id (never the intent contents), so a replay or a
        # forged claim leaves a record exactly as a granted one does.
        _audit(
            operation,
            session_key,
            "denied",
            f"tool_call_id={tool_call_id} no matching approved tool call",
        )
        return web.json_response(
            {"error": "no matching approved tool call", "code": "approved_call_required"},
            status=403,
        )
    _audit(operation, session_key, "ok", f"tool_call_id={tool_call_id}")
    return web.json_response({"capability": capability})


async def api_mediated_secret_request(request: web.Request) -> web.Response:
    """Perform an owner-authorized outbound HTTPS request carrying a Custom secret.

    The in-sandbox tool cannot read the vault (``.vault`` is masked in the agent's
    mount namespace), so it forwards the non-secret intent here, the trust
    boundary that already resolves ``secret://`` env references. The response
    returned is the fixed withheld constant from :func:`perform_mediated_request`
    and never contains the secret or anything the origin sent.
    """
    operation = "mediated_secret_request"
    session_key, payload, refusal = await _admit(request, operation)
    if refusal is not None:
        return refusal
    assert payload is not None
    capability = request.headers.get("X-Mediated-Secret-Capability", "").strip()
    if not mediated_request_capability.consume(session_key, capability, payload):
        _audit(
            operation,
            session_key,
            "denied",
            "missing, invalid, or replayed approved-call capability",
        )
        return web.json_response(
            {
                "error": "an unused capability from the approved tool call is required",
                "code": "approved_call_capability_required",
            },
            status=403,
        )
    secret_name = payload.get("secret_name")
    if not isinstance(secret_name, str) or not secret_name:
        return web.json_response(
            {"error": "secret_name is required", "code": "secret_name_required"}, status=400
        )

    # The mediation stack pulls the vault (``cryptography``), whose import is
    # synchronous and heavy, so the module import runs in a worker thread. The
    # dispatch itself is async: its blocking steps (policy read, DNS vet, vault
    # read) each run off the loop inside it.
    _dispatch = await asyncio.to_thread(
        importlib.import_module, "kiro_crew.secrets_mediation.dispatch"
    )
    req = _dispatch.MediatedRequest(
        secret_name=secret_name,
        method=str(payload.get("method", "")),
        url=str(payload.get("url", "")),
        headers=payload.get("headers") or {},
        query=payload.get("query") or {},
        json_body=payload.get("json_body"),
        timeout_s=float(payload.get("timeout_s") or 20.0),
    )
    outcome: str
    result: Any
    try:
        outcome, result = "ok", await _dispatch.perform_mediated_request(req, _config_dir())
    except (_dispatch.PolicyError, _dispatch.MediationError) as exc:
        # Safe, secret-free message; the value never appears in these.
        outcome, result = "refused", str(exc)
    if outcome == "refused":
        # Pre-send refusals never sent the secret, so they answer a distinct 400
        # and audit ``denied`` by the secret NAME only, never the value.
        _audit(operation, session_key, "denied", f"secret={secret_name}")
        return web.json_response({"error": result, "code": "mediation_refused"}, status=400)

    # Every post-send result is the fixed withheld response (a success, an SSRF
    # refusal at connect, a transport error, a redirect refusal and a timeout are
    # byte-identical), so the audit outcome is a constant ``ok`` too:
    # ``security_events.jsonl`` is readable inside the sandbox, and a distinct
    # outcome there would leak the one bit the withheld constant closes.
    _audit(operation, session_key, "ok", f"secret={secret_name} status={result.status}")
    return web.json_response(
        {
            "status": result.status,
            "headers": result.headers,
            "body": result.body,
            "truncated": result.truncated,
            "final_url_origin": result.final_url_origin,
        }
    )
