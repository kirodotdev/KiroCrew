"""Connections handlers for browser-to-gateway OAuth callback recovery."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from aiohttp import web

from kiro_crew.connections import get_provider
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


async def _relay_loopback_callback(callback: _LoopbackCallback) -> int:
    """Send one GET to a fixed loopback host and return its HTTP status."""
    host = "::1" if callback.ipv6 else "127.0.0.1"
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, callback.port),
        timeout=3,
    )
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
