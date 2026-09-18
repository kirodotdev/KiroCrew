"""``kirocrew-secrets`` — a trusted, always-shipped MCP server that lets an agent
use a Custom secret to call an owner-authorized API WITHOUT ever seeing the
plaintext.

This is a first-party core server (peer of ``kirocrew-core`` / ``-computer`` /
``-cron`` / ``-dashboard``), NOT an installable app: the trust guarantee — an
agent may *use* an explicitly authorized secret but can never read, print, log,
or forward its plaintext — depends on the mediation being trusted code that is
always present and cannot be toggled or reconfigured through the agent.

The single tool ``call_api_with_secret`` takes a secret NAME plus non-secret
request intent. This server runs in the agent's MCP runtime -- directly inside
the agent sandbox (where the vault is bind-mount-hidden and unreadable), or,
when routed through gatewayd's pooled backend, in a host-side pool process
without a mount namespace. In neither case does it RESOLVE the secret: it holds
only the secret NAME and does NOT touch the value. It
forwards the non-secret request intent over loopback to the host dashboard
process (``POST /api/mediated-secret-request``), the same trust boundary that
already resolves ``secret://`` env references. There, trusted code
(:mod:`kiro_crew.secrets_mediation.dispatch`) checks the owner's per-secret
origin+placement authorization, SSRF-validates and DNS-pins the destination,
resolves the secret only at dispatch, injects it, runs a bounded request, and
returns only a sanitized response. The value never enters this sandboxed
process, the tool arguments, the result, error text, logs, telemetry, or the
SEL audit record.

Deliberately NO ``autoApprove`` for this server (see ``agent._MANAGED_MCP_SERVERS``):
the call must still reach ``hooks.on_tool_call`` so the governance ceiling and
approval gate apply to a credential-bearing egress.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from kiro_crew.config.loader import KiroCrewConfig, read_local_secret
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.dashboard.urls import dashboard_socket_path
from kiro_crew.loopback_http import unix_socket_urlopen
from kiro_crew.mcp_caller import current_caller
from kiro_crew.mcp_shared import (
    call_tool_with_logging,
    member_proof_header_value,
    run_mcp_stdio_loop,
)
from kiro_crew.member_memory_auth import (
    PROOF_HEADER,
    issue_member_session_proof,
    protected_member_session_for_pid,
)
from kiro_crew.validation import validate_mcp_tool_arguments

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-secrets"
SERVER_VERSION = "1.0.0"

_TOOL = "call_api_with_secret"

#: The methods the tool advertises. Kept here (rather than imported from the
#: host-only dispatch module) so the sandboxed server has NO import path to the
#: vault: the schema is metadata, and the host endpoint re-validates the method.
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})


def _list_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": _TOOL,
            "description": (
                "Call an owner-authorized external HTTPS API using a stored Custom secret "
                "WITHOUT receiving the secret's value. You pass the secret's NAME (not its "
                "value) and the request details; Kiro Crew resolves the secret, injects it "
                "into the request per the owner's authorization, performs the call, and "
                "returns only a sanitized result. By DEFAULT that result is a fixed constant that reveals nothing about the upstream response -- no body, headers, status, content-type, or length -- so an authorized origin cannot echo the injected credential back to you through any channel. The owner opts a specific origin into receiving the real response (body + headers + status) with `secrets authorize --return-body`. The secret can be used ONLY against "
                "the exact https origin the owner authorized for it — you cannot choose an "
                "arbitrary destination. If the secret is not authorized for the URL's origin, "
                "the call is refused before the secret is read. The value never appears in "
                "your transcript, tool output, logs, or errors. The owner authorizes a "
                "secret's origins with `kirocrew secrets authorize` (owner-signed)."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["secret_name", "method", "url"],
                "properties": {
                    "secret_name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 256,
                        "description": "The exact name of the stored Custom secret to use.",
                    },
                    "method": {
                        "type": "string",
                        "enum": sorted(ALLOWED_METHODS),
                        "description": "HTTP method.",
                    },
                    "url": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2048,
                        "description": (
                            "Full https:// request URL. Its origin (scheme+host+port) must "
                            "exactly match the origin the owner authorized for this secret."
                        ),
                    },
                    "headers": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "Optional non-secret request headers. The credential header is "
                            "added by Kiro Crew, not here; a header that would collide with "
                            "it or set framing/Host is dropped."
                        ),
                    },
                    "query": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Optional non-secret query-string parameters.",
                    },
                    "json_body": {
                        "description": "Optional JSON request body (object, array, or scalar).",
                    },
                    "timeout_s": {
                        "type": "number",
                        "minimum": 0.1,
                        "maximum": 60,
                        "description": "Optional request timeout in seconds (default 20, max 60).",
                    },
                },
            },
        }
    ]


_INPUT_SCHEMAS: dict[str, Any] = {t["name"]: t.get("inputSchema") for t in _list_tools()}


def _validate_args(name: str, raw_args: dict[str, Any]) -> dict[str, Any]:
    args = raw_args or {}
    validate_mcp_tool_arguments(args, _INPUT_SCHEMAS.get(name))
    return args


def _resolve_session_key() -> str:
    """The caller's session key, for the host endpoint's X-Session-Key auth.

    Prefers the VERIFIED caller-meta session key from ``current_caller()`` — the
    authoritative routing identifier the gateway injects for a pooled/host-side
    backend. On that path the process PID and ``KIROCREW_SESSION_KEY`` env belong
    to the shared pool worker, NOT the member caller, so a PID/env-first
    resolution would send a key that does not match the gateway-injected proof's
    ``s`` and the endpoint would 403 the proxied member call. Falls back to the
    per-PID protected member session (direct protected topology) and then the
    ``KIROCREW_SESSION_KEY`` env (managed MCP subprocess). Returns "" when none is
    available; the host endpoint then refuses.
    """
    try:
        caller = current_caller()
        if caller and caller.session_key:
            return caller.session_key
    except Exception:  # noqa: BLE001 — fall back to PID/env resolution
        pass
    try:
        protected = protected_member_session_for_pid(os.getpid())
        if protected:
            return protected
    except Exception:  # noqa: BLE001 — fall back to the env key
        pass
    return os.environ.get("KIROCREW_SESSION_KEY", "")


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    if name != _TOOL:
        return f"Error: Unknown tool: {name}"

    session_key = _resolve_session_key()
    if not session_key:
        return "Error: This session could not be identified, so the request was refused."

    try:
        cfg = KiroCrewConfig.load()
        _host, port = parse_dashboard_url(cfg.dashboard.url)
        # Pin the IPv4 loopback literal, never the name ``localhost``: the gateway
        # listens on 127.0.0.1, but ``localhost`` can resolve to ``::1`` first,
        # where another local account could bind the same port and receive the
        # X-Internal-Secret + session key this request carries. A literal address
        # cannot be redirected by a hosts-file or resolver entry.
        api_base = f"http://127.0.0.1:{port}"
        secret = ""
        try:
            secret = read_local_secret(port)
        except Exception:  # noqa: BLE001 — endpoint refuses without it
            pass

        body = json.dumps(
            {
                "secret_name": args["secret_name"],
                "method": args["method"],
                "url": args["url"],
                "headers": dict(args.get("headers") or {}),
                "query": dict(args.get("query") or {}),
                "json_body": args.get("json_body"),
                "timeout_s": float(args.get("timeout_s") or 20.0),
            }
        ).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "X-Internal-Secret": secret,
            "X-Session-Key": session_key,
        }
        # Bind this credential egress to an approved member-session invocation via
        # the audience-bound member-session proof (HMAC'd with the sandbox-masked
        # member key; a raw in-sandbox `python -c` cannot mint it → 403). Two
        # sources, in order:
        #   1. the gateway-injected per-call proof in the caller meta (pooled path);
        #   2. a self-minted proof from THIS process's own protected-member pid +
        #      session, for a DIRECT member MCP call the gateway did not proxy —
        #      without this that flow has no proof and is refused (unreachable).
        # Both are equally unforgeable: security comes from the masked signing key,
        # not from which process mints the proof, and issue_member_session_proof
        # returns "" for any pid/session that is not a genuine protected member.
        proof = ""
        try:
            caller = current_caller()
            if caller and caller.member_memory_proof:
                proof = member_proof_header_value(caller.member_memory_proof)
            if not proof and session_key:
                proof = member_proof_header_value(
                    issue_member_session_proof(session_key, os.getpid(), audience=SERVER_NAME)
                )
            if proof:
                headers[PROOF_HEADER] = proof
        except Exception:  # noqa: BLE001 — absence is handled by the endpoint (403)
            pass
        req = urllib.request.Request(
            f"{api_base}/api/mediated-secret-request",
            data=body,
            headers=headers,
            method="POST",
        )
        # The host dispatch enforces ONE end-to-end deadline bounded by the
        # request's timeout_s (max 60s), across all redirect hops. Give the
        # loopback hop a margin beyond that ceiling so a slow-but-valid upstream
        # is cut off by the dispatch's own deadline, never by this outer layer.
        #
        # Transport: the owner-only dashboard unix socket, with NO TCP fallback.
        # This request carries the X-Internal-Secret, the session key, and the
        # member-session proof — a co-located lower-privileged user who binds the
        # released loopback PORT during a gateway restart would otherwise receive
        # them. ``unix_socket_urlopen`` has no TCP handler at all, so "no fallback"
        # is structural: a missing/stale socket raises rather than sending the
        # credential over a port another user could answer. The socket lives in
        # the owner-only data home (chmod 0600, SO_PEERCRED-verified server-side).
        with unix_socket_urlopen(req, timeout=75, socket_path=dashboard_socket_path(port)) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # The host endpoint returns a safe, secret-free JSON error for refusals.
        try:
            detail = json.loads(exc.read()).get("error", "")
        except Exception:  # noqa: BLE001
            detail = ""
        return f"Error: {detail or 'The mediated request was refused.'}"
    except Exception:  # noqa: BLE001 — never leak internals; never crash the server
        logger.warning("call_api_with_secret failed to reach the mediation endpoint", exc_info=True)
        return "Error: The mediated request could not be completed."

    return json.dumps(
        {
            "status": payload.get("status"),
            "headers": payload.get("headers", {}),
            "body": payload.get("body", ""),
            "truncated": payload.get("truncated", False),
            "origin": payload.get("final_url_origin", ""),
        },
        indent=2,
    )


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        session_key=f"mcp_{SERVER_NAME}",
        downstream_service=SERVER_NAME,
    )


#: Advertise caller identity: the gateway injects the per-call
#: ``_meta.kirocrew.caller`` block (which carries the audience-bound member
#: proof this tool forwards to the host endpoint) ONLY into a backend that
#: advertised this capability. A credential-bearing egress MUST be caller-aware,
#: so an unidentifiable caller cannot drive it. Mirrored in
#: ``mcp_discovery._MANAGED_SERVERS_CALLER_AWARE`` (pinned by
#: ``test_mcp_managed_caller_identity.py``).
ADVERTISE_CALLER_IDENTITY = True


def run_mcp_server() -> None:
    """Run the stdio MCP server. Entry point for ``kirocrew mcp-secrets``."""
    logging.basicConfig(level=os.environ.get("KIROCREW_LOG_LEVEL", "WARNING"), stream=None)
    run_mcp_stdio_loop(
        SERVER_NAME,
        SERVER_VERSION,
        _list_tools,
        _call_tool,
        advertise_caller_identity=ADVERTISE_CALLER_IDENTITY,
    )
