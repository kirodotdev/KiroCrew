"""The proxy-safe spelling of a crew name in an ``/api/agents/...`` URL path.

A crew name such as ``Nova Sky`` is percent-encoded into the path as
``Nova%20Sky``. A reverse proxy that normalises paths decodes ``%20`` back to a
literal space, which is the request-line delimiter, and the request dies with a
502 before it reaches the gateway. ``PUT /api/agents`` takes the name in the
JSON body instead; the routes below cannot all do that (a GET
has no body, the avatar upload is multipart, a DELETE body is dropped by some
proxies), so each one also answers under a base64url segment that contains
only ``[A-Za-z0-9_-]``:

    /api/agents/{name}/capabilities   ->  /api/agents/b64/{name_b64}/capabilities

The base64url routes reach the SAME handlers as their path-form twins, so the
owner gate and everything after it are unchanged; only where the name is read
from differs (``dashboard.agent_name_segment.agent_route_name``). No twin shares a method and
segment pattern with a path-form ``/api/agents/{name}/...`` route, so a crew
that is really named ``b64`` keeps its own routes (aiohttp tries the longer
static prefix first, so registration order alone would not guarantee it).

The encoding is canonical: unpadded base64url of the UTF-8 bytes. A segment
that does not re-encode to itself (padding, a non-canonical last character,
bytes that are not UTF-8) is refused, so one crew name has exactly one
base64url spelling.
"""

from __future__ import annotations

import base64
import binascii
import string
import urllib.parse

#: The base64url twin of each path-form agent route template, as registered.
#: The change-card hook reads a base64url request as its path-form template
#: (``change_cards.route_key``) so a card step, its memory line and its undo
#: guard see one route whichever spelling the browser used.
B64_ROUTE_ALIASES: dict[str, str] = {
    "/api/agents/b64/{name_b64}": "/api/agents/{name}",
    "/api/agents/b64/{name_b64}/avatar": "/api/agents/{name}/avatar",
    "/api/agents/b64/{name_b64}/capabilities": "/api/agents/{name}/capabilities",
    "/api/agents/b64/{name_b64}/capabilities/preview": "/api/agents/{name}/capabilities/preview",
    "/api/agents/b64/{name_b64}/capabilities/detach": "/api/agents/{name}/capabilities/detach",
    "/api/agents/b64/{name_b64}/worker-kit": "/api/agents/{name}/worker-kit",
    "/api/agents/b64/{name_b64}/worker-kit/preview": "/api/agents/{name}/worker-kit/preview",
}

_SEGMENT_CHARS = frozenset(string.ascii_letters + string.digits + "-_")


def encode_agent_name(name: str) -> str:
    """The canonical base64url segment for *name* (unpadded)."""
    return base64.urlsafe_b64encode(name.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_agent_name(segment: str) -> str | None:
    """The crew name *segment* spells, or ``None`` when it is not a canonical spelling."""
    if not isinstance(segment, str) or not segment or len(segment) % 4 == 1:
        return None
    if not all(c in _SEGMENT_CHARS for c in segment):
        return None
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        name = raw.decode("utf-8")
    except (binascii.Error, ValueError):
        return None
    if not name or encode_agent_name(name) != segment:
        return None
    return name


def needs_proxy_safe_path(name: str) -> bool:
    """Whether *name* would be percent-encoded in a URL path segment.

    Matches the dashboard's ``agentPath`` (``website/src/api/agentPath.ts``),
    which also treats ``!'()*`` as needing the base64url form, so both sides
    spell a name the same way.
    """
    return urllib.parse.quote(name, safe="") != name


def agent_path(name: str, suffix: str = "") -> str:
    """``/api/agents/<name><suffix>``, in the base64url spelling when the name needs encoding."""
    if needs_proxy_safe_path(name):
        return f"/api/agents/b64/{encode_agent_name(name)}{suffix}"
    return f"/api/agents/{name}{suffix}"


def agent_record_write(name: str, body: dict) -> tuple[str, dict]:
    """``(path, body)`` of the crew-record ``PUT``: ``PUT /api/agents`` with the name in
    the body when the name needs encoding, else ``PUT /api/agents/<name>``."""
    if needs_proxy_safe_path(name):
        return "/api/agents", {**body, "name": name}
    return f"/api/agents/{name}", body
