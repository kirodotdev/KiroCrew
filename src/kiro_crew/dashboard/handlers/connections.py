"""Connections handlers for browser-to-gateway OAuth callback recovery."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from aiohttp import web

from kiro_crew.connections import get_provider
from kiro_crew.connections.registry import Provider
from kiro_crew.sel import sel

_MAX_RETURN_ADDRESS_BYTES = 8192
_MAX_REQUEST_TARGET_BYTES = 6144
_SERVER_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ALLOWED_CALLBACK_QUERY_KEYS = {
    "authuser",
    "code",
    "error",
    "error_description",
    "iss",
    "prompt",
    "scope",
    "state",
}


@dataclass(frozen=True)
class _LoopbackCallback:
    """A validated callback reduced to the fields needed for a fixed-host GET."""

    port: int
    request_target: str
    ipv6: bool = False


def _validated_loopback_return_address(value: object) -> _LoopbackCallback | None:
    """Parse a browser return address into a constrained loopback callback.

    The user controls only an unprivileged loopback port and an ASCII HTTP
    request-target containing a single OAuth code.  The network host is selected
    later from fixed literals, so request data can never choose a remote host.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate.encode("utf-8")) > _MAX_RETURN_ADDRESS_BYTES:
        return None
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or host not in {"127.0.0.1", "::1", "localhost"}
        or port is None
        or port < 1024
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None

    query = parse_qs(parsed.query, keep_blank_values=True)
    codes = query.get("code", [])
    contains_control = any(
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        for values in query.values()
        for value in values
    )
    if (
        len(codes) != 1
        or not codes[0]
        or not set(query).issubset(_ALLOWED_CALLBACK_QUERY_KEYS)
        or contains_control
    ):
        return None

    path = parsed.path or "/"
    request_target = path + (f"?{parsed.query}" if parsed.query else "")
    if (
        not request_target.isascii()
        or any(character in request_target for character in "\r\n ")
        or len(request_target.encode("ascii")) > _MAX_REQUEST_TARGET_BYTES
    ):
        return None
    return _LoopbackCallback(
        port=port,
        request_target=request_target,
        ipv6=host == "::1",
    )


class _NoListener(Exception):
    """Nothing is bound to the loopback port a return address names."""


async def _relay_loopback_callback(callback: _LoopbackCallback) -> int:
    """Send one GET to a fixed loopback host and return its HTTP status."""
    host = "::1" if callback.ipv6 else "127.0.0.1"
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, callback.port),
            timeout=3,
        )
    except ConnectionRefusedError as refused:
        # The kernel answered for this port and said nothing is bound to it. That
        # is the only signal proving the listener is ABSENT rather than merely
        # slow, saturated or unroutable, so it is the only one raised distinctly:
        # every other dial failure stays an ordinary delivery failure. Once the
        # connection is established the listener demonstrably exists, so nothing
        # after this point may reach here either.
        raise _NoListener(str(refused)) from refused
    try:
        host_header = f"[{host}]" if callback.ipv6 else host
        request = (
            f"GET {callback.request_target} HTTP/1.1\r\n"
            f"Host: {host_header}:{callback.port}\r\n"
            "Connection: close\r\n"
            "Accept: text/plain\r\n\r\n"
        ).encode("ascii")
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout=3)
        status_line = await asyncio.wait_for(reader.readline(), timeout=5)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})[^\r\n]*\r?\n", status_line)
    if match is None:
        raise OSError("OAuth callback returned an invalid HTTP status line")
    return int(match.group(1))


def _bad_request(error: str, code: str) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=400)


def _bad_gateway(error: str, code: str) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=502)


def _approval_superseded(error: str, code: str) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=409)


async def api_mcp_oauth_relay(request: web.Request) -> web.Response:
    """POST /api/mcp/oauth/relay — deliver a failed browser redirect locally."""
    try:
        body = await request.json()
    except Exception:
        return _bad_request("invalid JSON", "invalid_json")
    if not isinstance(body, dict):
        return _bad_request("request body must be an object", "invalid_request_body")

    server = body.get("server")
    if (
        not isinstance(server, str)
        or not _SERVER_SLUG_RE.fullmatch(server)
        or get_provider(server) is None
    ):
        return _bad_request("invalid server", "invalid_server")
    callback = _validated_loopback_return_address(body.get("redirect_url"))
    if callback is None:
        return _bad_request(
            "invalid loopback return address",
            "invalid_loopback_return_address",
        )

    try:
        callback_status = await _relay_loopback_callback(callback)
    except _NoListener:
        # Nothing is bound to that port. The listener and the PKCE verifier are
        # created by the process that minted the authorize URL and die with it, so
        # its absence proves the code can no longer be redeemed BY ANYONE -- a
        # fresh listener on the same port never saw the verifier. Answering with
        # the delivery-failure message below would blame the paste for an
        # approval that is simply spent.
        sel().log_api_access(
            caller="dashboard",
            operation="mcp_oauth_callback_relay",
            outcome="denied",
            resources=server,
        )
        return _approval_superseded(
            "the approval this return address belongs to is no longer live",
            "approval_superseded",
        )
    except (asyncio.TimeoutError, OSError, ValueError):
        sel().log_api_access(
            caller="dashboard",
            operation="mcp_oauth_callback_relay",
            outcome="failed",
            resources=server,
        )
        return _bad_gateway(
            "the local OAuth callback did not accept the return address",
            "oauth_callback_unreachable",
        )

    if callback_status >= 400:
        sel().log_api_access(
            caller="dashboard",
            operation="mcp_oauth_callback_relay",
            outcome="failed",
            resources=server,
        )
        return _bad_gateway(
            "the local OAuth callback rejected the return address",
            "oauth_callback_rejected",
        )

    sel().log_api_access(
        caller="dashboard",
        operation="mcp_oauth_callback_relay",
        outcome="completed",
        resources=server,
    )
    return web.json_response({"ok": True})


# ── On-demand approval-URL mint ──
#
# Connect asks for a URL instead of waiting for one. The engine lives in
# kiro_crew.connections.mint; these two handlers are its HTTP surface, and the
# GET is the card's authoritative feed for a card-initiated mint.

# Fire-and-forget mint tasks, held so the loop cannot collect one mid-flight.
_mint_tasks: set[asyncio.Task] = set()


def _requested_provider(slug: str) -> Provider | None:
    """The registry provider ``slug`` names, or None."""
    if not slug or len(slug) > 64 or not _SERVER_SLUG_RE.match(slug):
        return None
    provider = get_provider(slug)
    if provider is None or not provider.get("mcp_url"):
        return None
    return provider


async def _mint_request(
    request: web.Request,
) -> tuple[dict, Provider] | web.Response:
    """The JSON body and its registry provider, or the error response to return.

    Registry membership is the bound on what a caller can make the gateway spawn: a
    mint starts a kiro-cli process, so the slug has to resolve to a provider we ship
    rather than to arbitrary caller-supplied text.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is a client error, not a fault
        return _bad_request("body must be JSON", "invalid_body")
    if not isinstance(body, dict):
        return _bad_request("body must be a JSON object", "invalid_body")
    slug = str(body.get("slug") or "").strip().lower()
    provider = _requested_provider(slug)
    if provider is None:
        return _bad_request("unknown provider", "unknown_provider")
    return body, provider


async def api_connections_mint(request: web.Request) -> web.Response:
    """POST /api/connections/mint — start minting a provider's approval URL.

    Returns as soon as the mint is scheduled. The URL is not ready yet: the
    caller polls :func:`api_connections_mint_state` for it.
    """
    parsed = await _mint_request(request)
    if isinstance(parsed, web.Response):
        return parsed
    _body, provider = parsed
    slug = str(provider["slug"])

    # Function-local by DESIGN, not for a cycle: this handlers package is imported
    # on the gateway boot path, and the mint engine drags in the ACP client, the
    # credential predicate and the PID registry. Keeping it here is what stops a
    # gateway start paying for a subsystem most requests never touch, and
    # test_the_handlers_package_does_not_import_the_mint_engine enforces it in a
    # subprocess -- hoisting this to module scope turns that test red.
    from kiro_crew.connections.mint import _dispose_mint, reserve_mint_row, start_oauth_mint

    # Reserved BEFORE responding: the response names a row this tab polls
    # immediately, so the row has to be visible first. Allocating only a token here
    # would leave the previous (possibly terminal) row answering that poll, and the
    # card would read it as the verdict on this attempt.
    token, prior = await reserve_mint_row(slug)
    try:
        task = asyncio.create_task(
            start_oauth_mint(slug, str(provider["mcp_url"]), token, prior)
        )
    except BaseException:
        # The flow owns the displaced row once it starts; if it never starts,
        # nothing else will ever release that row's process and spec.
        if prior is not None:
            await _dispose_mint(prior)
        raise
    _mint_tasks.add(task)
    task.add_done_callback(_mint_tasks.discard)

    # Off the loop: only the append is queued to SEL's writer thread. The FIRST
    # sel() of a process CONSTRUCTS the log -- trust-dir creation, key validation,
    # and on Windows an icacls subprocess -- and this handler runs BEFORE the audit
    # middleware's own call (that one logs the response), so on a fresh gateway
    # whose first state-changing request is a Connect click it would land here and
    # stall every other request. Same reasoning as server._audit_denied.
    await asyncio.to_thread(
        lambda: sel().log_api_access(
            caller="dashboard",
            operation="connections_mint",
            outcome="started",
            resources=f"provider:{slug}",
        )
    )
    return web.json_response({"ok": True, "slug": slug, "state": "minting", "token": token})


async def api_connections_mint_state(request: web.Request) -> web.Response:
    """GET /api/connections/mint?slug=… — this provider's mint state and URL.

    ``idle`` means no mint exists for the provider, which is distinct from a mint
    that ran and produced nothing: the card treats it as "nothing pending" rather
    than as a failure.
    """
    slug = str(request.query.get("slug") or "").strip().lower()
    if _requested_provider(slug) is None:
        return _bad_request("unknown provider", "unknown_provider")

    # Function-local for the same reason as the POST above: the boot path must not
    # carry the mint engine, and the subprocess guard test enforces it.
    from kiro_crew.connections.mint import expire_dead_holder, pending_mint_for

    # Commit the dead-holder verdict before reporting it, so the row the abandon
    # fence sees matches the state this response hands the card.
    await expire_dead_holder(slug)
    view = pending_mint_for(slug)
    if view is None:
        return web.json_response({"slug": slug, "state": "idle"})
    payload: dict[str, object] = {"slug": slug, "state": view.get("state", "minting")}
    if view.get("token"):
        payload["token"] = view["token"]
    if view.get("oauth_url"):
        payload["oauth_url"] = view["oauth_url"]
    if view.get("reason"):
        payload["reason"] = view["reason"]
    return web.json_response(payload)
