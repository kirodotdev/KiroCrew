"""Request-side helpers for the base64url crew routes; the encoding is :mod:`kiro_crew.agent_url_path`."""

from __future__ import annotations

from aiohttp import web

from kiro_crew.agent_url_path import decode_agent_name


def agent_route_name(request: web.Request) -> str | None:
    """The crew name a request addresses: its ``{name}`` segment, or its decoded ``{name_b64}``.

    ``None`` only for a ``{name_b64}`` segment that is not a canonical spelling.
    """
    if "name" in request.match_info:
        return request.match_info["name"]
    return decode_agent_name(request.match_info.get("name_b64", ""))


def invalid_agent_name_response() -> web.Response:
    """The 400 a malformed ``{name_b64}`` segment gets."""
    return web.json_response(
        {"error": "invalid crew name segment", "code": "name_segment_invalid"}, status=400
    )
