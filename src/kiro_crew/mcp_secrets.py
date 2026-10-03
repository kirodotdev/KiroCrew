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

from kiro_crew import platform_compat
from kiro_crew.config.loader import KiroCrewConfig, read_local_secret
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.dashboard.urls import dashboard_socket_path
from kiro_crew.instances import run_marker
from kiro_crew.loopback_http import unix_socket_urlopen
from kiro_crew.mcp_caller import current_caller, current_tool_call_id
from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self, get_peer_pid
from kiro_crew.mcp_shared import (
    call_tool_with_logging,
    run_mcp_stdio_loop,
)
from kiro_crew.mediated_request_capability import MAX_REQUEST_PAYLOAD_BYTES
from kiro_crew.member_memory_auth import (
    protected_member_session_for_pid,
)
from kiro_crew.session_token_sig import session_token_header
from kiro_crew.validation import validate_mcp_tool_arguments

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-secrets"

#: Fail-closed ceilings on the request collections this tool retains before it
#: copies/serializes them, so a caller cannot exhaust the MCP worker's memory.
#: Item COUNT is not expressible in the declared JSON schema, so it is enforced
#: in code; the total-payload byte ceiling bounds json_body and the maps together
#: (headers/query leaf strings and depth are separately capped by the validator).
_MAX_MAP_ITEMS = 256
#: Alias of the single mediated-request payload ceiling, so the tool's send-side
#: cap and the host endpoints' receive-side cap are the same 1 MiB value.
_MAX_REQUEST_PAYLOAD_BYTES = MAX_REQUEST_PAYLOAD_BYTES
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
                "returns only a sanitized result. That result is a fixed constant that reveals nothing about the upstream response -- no body, headers, status, content-type, or length -- so an authorized origin cannot echo the injected credential back to you through any channel. The tool lets you USE the secret against the owner-authorized origin; it never returns the origin's response. The secret can be used ONLY against "
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
    ``KIROCREW_SESSION_KEY`` env (managed MCP subprocess). The per-PID rung honours
    the canonical resolver contract: a non-None protected value STOPS the ladder,
    so an explicit revoked/invalid record ("") is returned as the hard-refusal
    blank the host endpoint 403s — never a fall-through to the ambient env key —
    and a raising probe likewise returns "" rather than being re-read as identity
    from the env the fenced process could itself write. The env key is consulted
    only when the binding is genuinely absent (protected is None). Returns "" when
    no identity is available or one was explicitly refused; the host endpoint then
    refuses.
    """
    try:
        caller = current_caller()
        if caller and caller.session_key:
            return caller.session_key
    except Exception:  # noqa: BLE001 — fall back to PID/env resolution
        pass
    try:
        protected = protected_member_session_for_pid(os.getpid())
        if protected is not None:
            # Non-None STOPS the ladder, matching the canonical resolver contract
            # (member_memory_auth.protected_member_session_for_pid): a host
            # override returns "" ONLY for an explicit invalid/revoked record, and
            # a blank is a HARD REFUSAL the caller must honour — the host endpoint
            # 403s a blank X-Session-Key rather than attesting it. Truthiness here
            # would let a revoked "" fall through to the ambient KIROCREW_SESSION_KEY
            # below, re-identifying a fenced process as the ambient session on a
            # credential-egress path. Return protected as-is, "" included.
            return protected
    except Exception:  # noqa: BLE001
        # A RAISING probe is a hard refusal too, NOT a fall-through: the same-uid
        # fence means an unreadable/denied binding must never be re-read as
        # identity from the ambient env key (which the fenced process can itself
        # write). Return "" so the host endpoint refuses, rather than swallowing
        # the error and sending KIROCREW_SESSION_KEY.
        return ""
    # Only a genuinely absent binding (protected is None) falls through to the
    # managed-MCP-subprocess env key.
    return os.environ.get("KIROCREW_SESSION_KEY", "")


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    if name != _TOOL:
        return f"Error: Unknown tool: {name}"

    # Explicit unsupported-platform result on Windows. The mediated request is
    # credential-safe ONLY because both the capability claim and the dispatch ride
    # an AF_UNIX loopback socket in the owner-only data home, whose answering peer
    # is attested by SO_PEERCRED (LOCAL_PEERPID) ancestry before any credential
    # byte is sent. On Windows the dashboard starts no Unix site and that
    # peer-credential channel does not exist, and no TCP fallback is offered:
    # sending the X-Internal-Secret + signed session token over a loopback PORT a
    # co-located lower-privileged account could bind would defeat the whole point.
    # Gate on IS_WINDOWS, not hasattr(socket, "AF_UNIX"): modern Windows Python
    # DOES expose AF_UNIX, so the attribute check would pass there and fall through
    # to a nonexistent Unix site. Refuse EXPLICITLY rather than let
    # unix_socket_urlopen raise a bare OSError the broad handler flattens.
    if platform_compat.IS_WINDOWS:
        return (
            "Error: custom-secret mediation is not supported on this platform. It "
            "requires a Unix-domain socket with peer-credential attestation, which "
            "is unavailable here; refusing rather than send a credential over a "
            "channel this platform cannot attest."
        )

    session_key = _resolve_session_key()
    if not session_key:
        return "Error: This session could not be identified, so the request was refused."

    # Bound every collection this tool RETAINS before it is copied/serialized, so
    # a caller cannot exhaust the MCP worker's memory with a huge headers/query
    # map or json_body. Per-leaf string length (1 MB) and nesting depth (16) are
    # already capped by the shared validator, but item COUNT is not expressible in
    # the declared JSON schema (maxProperties is not a supported keyword), so the
    # count and total-size ceilings are enforced here and fail closed.
    _headers = args.get("headers") or {}
    _query = args.get("query") or {}
    if isinstance(_headers, dict) and len(_headers) > _MAX_MAP_ITEMS:
        return f"Error: too many headers (>{_MAX_MAP_ITEMS}); the request was refused."
    if isinstance(_query, dict) and len(_query) > _MAX_MAP_ITEMS:
        return f"Error: too many query parameters (>{_MAX_MAP_ITEMS}); the request was refused."
    try:
        _payload_probe = json.dumps(
            {
                "headers": dict(_headers),
                "query": dict(_query),
                "json_body": args.get("json_body"),
            }
        )
    except (TypeError, ValueError):
        return "Error: the request body is not serializable; the request was refused."
    if len(_payload_probe.encode("utf-8")) > _MAX_REQUEST_PAYLOAD_BYTES:
        return (
            f"Error: the request payload exceeds {_MAX_REQUEST_PAYLOAD_BYTES} bytes; "
            "the request was refused."
        )

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

        request_payload = {
            "secret_name": args["secret_name"],
            "method": args["method"],
            "url": args["url"],
            "headers": dict(args.get("headers") or {}),
            "query": dict(args.get("query") or {}),
            "json_body": args.get("json_body"),
            "timeout_s": float(args.get("timeout_s") or 20.0),
        }
        body = json.dumps(request_payload).encode("utf-8")

        # Attach the signed per-session token alongside X-Session-Key, exactly as
        # every other Kiro Crew control-plane server does. It is the SECOND
        # attestation channel ``session_key_is_attested`` accepts: on a POOLED MCP
        # backend the peer ancestry does not resolve (``peer_verified`` is never
        # set), so without this token the endpoint could not attest the caller and
        # would refuse every pooled call. The token is read from this process's
        # env when spawned per-session, or from the per-call caller block when
        # gatewayd forwards it to a pooled control-plane server (the caller-block
        # value wins because it is stamped per call and cannot go stale). It is a
        # transport-identity proof the gateway signs and verifies, not a header a
        # sandboxed process can forge.
        _ctx = current_caller()
        _tok = _ctx.session_token if _ctx is not None and _ctx.from_gateway else ""
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Secret": secret,
            "X-Session-Key": session_key,
            **session_token_header(_tok),
        }
        # No client-minted proof header: the host endpoint authenticates this
        # request itself via ``member_request_scope`` — the same transport
        # attestation the member-memory routes use. Trust comes from the
        # owner-only Unix socket's kernel peer attestation OR the signed
        # per-session token attached above, never from a bearer a raw in-sandbox
        # `python -c` could mint — it holds neither channel, so it presents no
        # attested member scope and is refused 403.
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
        # Verify the process ANSWERING the socket is the genuine gateway before
        # sending any credential bytes. The socket path lives in the owner-writable
        # data home, so a confined same-UID agent subprocess could unlink it and
        # bind its own listener there, then receive the X-Internal-Secret + signed
        # session token this request carries. The kernel names the listener's pid
        # as of listen() (SO_PEERCRED / LOCAL_PEERPID), which no listener can fake.
        #
        # An ancestor-walk is NOT sufficient: the untrusted ACP harness that hosts
        # this tool is its PARENT by construction, so it is an ancestor on the
        # first step of any such walk. A harness that rebinds the socket would be
        # admitted by "is the peer any ancestor of mine." The answerer must instead
        # be the SPECIFIC gateway process, whose identity the gateway itself
        # publishes to the owner-only ``run/`` sidecar (``0600`` inside the ``0700``
        # keystone-fenced dir, on the ``is_sensitive_path`` floor, so neither the
        # harness nor an agent file tool can write it): the gateway pid plus its
        # start-token. The peer must equal that published pid AND that pid's live
        # start-token must still match the recorded one, which proves the pid names
        # the same process the gateway recorded — defeating a rebinding harness
        # (different pid) and a recycled pid number (different start-token).
        #
        # Deny-by-default: an unreadable peer, an absent/unverifiable published
        # identity, a pid mismatch, or an unproven start-token all refuse before
        # the bytes go out. This mirrors the server-side SO_PEERCRED admission and
        # the pod runtime's own attested-gateway verifier.

        def _verify_gateway_peer(sock: Any) -> None:
            # FIRST, anchor trust to the KERNEL-attested peer principal, which no
            # file rewrite can forge. The run-marker checks below read the EXPECTED
            # gateway identity (pid + start-token) from a file in the owner-writable
            # run/ dir; a same-uid confined process could rewrite that record to its
            # OWN pid and rebind the socket, so the pid-match + start-token checks
            # would then pass against a rogue listener the agent planted. The one
            # proof the agent cannot forge is the OS-reported owner of the connected
            # socket: SO_PEERCRED (LOCAL_PEERCRED) is read in THIS process's kernel,
            # not from any file, so it refuses a peer that is not our own principal
            # — closing the cross-account rebind (a co-located lower-privileged user
            # who bound the path) that the loopback-port comments above also cite.
            # The server side already admits on this same check; the client must too
            # before sending any credential byte.
            principal = check_peer_is_self(sock)
            if principal is not PeerCredResult.MATCH:
                raise PermissionError(
                    "the dashboard socket peer is not confirmed to be this user's "
                    f"own gateway principal (peer check: {principal.value}); the "
                    "socket path may have been rebound by another account. Refusing "
                    "to send the mediated-secret credential."
                )
            peer = get_peer_pid(sock)
            if peer is None:
                raise PermissionError(
                    "cannot read the peer credentials of the dashboard socket; "
                    "refusing to send the mediated-secret credential to an "
                    "unverifiable listener."
                )
            record = run_marker.read_pid_record_path(run_marker.pid_path(port))
            if record is None:
                raise PermissionError(
                    "the gateway published no run-marker pid for this port, so the "
                    "socket peer cannot be authenticated as the genuine gateway. "
                    "Refusing to send the mediated-secret credential."
                )
            gateway_pid, recorded_start = record
            if peer != gateway_pid:
                raise PermissionError(
                    f"the dashboard socket is answered by pid {peer}, not the "
                    f"gateway pid {gateway_pid} the run-marker records; the socket "
                    "path may have been rebound. Refusing to send the "
                    "mediated-secret credential."
                )
            # The pid matched the record, but a pid number can be recycled: require
            # the peer pid's CURRENT start-token to equal the one the gateway
            # recorded. An empty token on either side is unproven, never a match
            # (the mediated path runs only on Linux/macOS, where the token is
            # available), so a recycled or unattestable pid is refused.
            live_start = run_marker.pid_start_token(peer)
            if not recorded_start or not live_start or live_start != recorded_start:
                raise PermissionError(
                    f"the dashboard socket peer pid {peer} does not carry the "
                    "start-identity the gateway recorded, so it cannot be proven to "
                    "be the same process; the pid may have been recycled. Refusing "
                    "to send the mediated-secret credential."
                )

        call_id = current_tool_call_id()
        if not call_id:
            return "Error: This tool call has no approved-call correlation, so the request was refused."
        claim_body = json.dumps({"tool_call_id": call_id, "request": request_payload}).encode(
            "utf-8"
        )
        claim_req = urllib.request.Request(
            f"{api_base}/api/mediated-secret-capability",
            data=claim_body,
            headers=headers,
            method="POST",
        )
        with unix_socket_urlopen(
            claim_req,
            timeout=5,
            socket_path=dashboard_socket_path(port),
            verify_peer=_verify_gateway_peer,
        ) as claim_resp:
            capability = str(json.loads(claim_resp.read()).get("capability") or "")
        if not capability:
            return "Error: This tool call was not approved for mediated secret use."
        req.add_header("X-Mediated-Secret-Capability", capability)

        with unix_socket_urlopen(
            req,
            timeout=75,
            socket_path=dashboard_socket_path(port),
            verify_peer=_verify_gateway_peer,
        ) as resp:
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
