"""The ``describe-session`` control frame: what a session's live stub registered with.

A resource-subscription holder (``rfc-mcp-resource-subscriptions.md`` §4.1) is a
connection the Kiro Crew server opens for a session, and it must land on the
same backend as the session's own stub, as the same caller. It cannot compute
the Register itself -- the PoolKey fields are hashed from the stub's launch
inputs -- so it asks the daemon for the Register and the ``initialize`` request
of the session's live stub for one server.

Every live registered connection is tracked here from the moment it is
accepted to its teardown. A reply never carries a session token (popped from
the frame before it is stored) or the stub's ``stub_uuid``: the holder mints its
own, because the daemon keys attach and routing on that id.

A Register or ``initialize`` over the retained-container bound is measured
before anything copies or keeps it, and is not kept: a same-uid peer may send
one up to the daemon's frame ceiling. ``describe-session`` for that connection
answers with the reason instead.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from kiro_crew.mcp_gateway.daemon.identity import _StubConn
from kiro_crew.mcp_gateway.resource_holder import RESOURCE_HOLDER_FIELD
from kiro_crew.mcp_gateway.resource_subscription_store import (
    RETAINED_CONTAINER_MAX_BYTES,
    serialized_container_size,
)

#: Live registered connections, oldest first. A dict for its insertion order:
#: the newest matching connection is the one a session started last.
_LIVE_CONNS: dict[_StubConn, None] = {}


def _oversized(field_name: str, container: dict[str, Any]) -> str:
    """Why *container* is not kept, or "" when it is within the bound."""
    size = serialized_container_size(container)
    if size <= RETAINED_CONTAINER_MAX_BYTES:
        return ""
    return (
        f"the session's {field_name} exceeds the retained-container limit of "
        f"{RETAINED_CONTAINER_MAX_BYTES} bytes ({size})"
    )


def track_live_conn(conn: _StubConn, register: dict[str, Any], *, private_backend: bool) -> None:
    """Remember *conn*, the Register it was accepted with, and how it was served."""
    conn.private_backend = private_backend
    conn.resource_holder = bool(register.get(RESOURCE_HOLDER_FIELD))
    # Measured as it is kept, without its two token fields, so the daemon, the
    # service and the store measure one size. A shallow copy: nothing nested
    # is copied before the bound is checked.
    retained = {
        key: value
        for key, value in register.items()
        if key not in ("stub_uuid", "stub_session_token")
    }
    conn.describe_refusal = _oversized("Register", retained)
    if conn.describe_refusal:
        conn.register = None
        # Resolved against the configured targets before the connection was
        # accepted, so it is a configured server name.
        server_name = register.get("server_name")
        conn.oversized_server = server_name if isinstance(server_name, str) else ""
    else:
        conn.register = copy.deepcopy(retained)
    _LIVE_CONNS[conn] = None


def track_initialize(conn: _StubConn, initialize: dict[str, Any]) -> None:
    """Keep the ``initialize`` *conn* sent, unless it is over the bound."""
    refusal = _oversized("initialize", initialize)
    if refusal:
        conn.captured_init = None
        conn.describe_refusal = conn.describe_refusal or refusal
    else:
        conn.captured_init = initialize


def forget_live_conn(conn: _StubConn) -> None:
    _LIVE_CONNS.pop(conn, None)


def _attributed(conn: _StubConn, session_key: str, server: str) -> bool:
    register = conn.register
    served = register.get("server_name") if register is not None else conn.oversized_server
    return (
        served == server
        and not conn.resource_holder
        and conn.caller is not None
        and conn.caller.session_key == session_key
    )


def _describes(conn: _StubConn, session_key: str, server: str) -> bool:
    return (
        conn.register is not None
        and not conn.describe_refusal
        and _attributed(conn, session_key, server)
        and conn.captured_init is not None
    )


def describe_session_reply(frame: dict[str, Any]) -> dict[str, Any]:
    """Answer ``{"type": "describe-session", "session_key", "server"}``.

    ``session-described`` carries ``register`` and ``initialize`` of the newest
    live connection that is attributed to the session, registered for the
    server and has sent ``initialize``. Anything else is ``session-undescribed``
    with a reason, so the caller can refuse a subscribe in words.
    """
    session_key = frame.get("session_key")
    server = frame.get("server")
    if (
        not isinstance(session_key, str)
        or not session_key
        or not isinstance(server, str)
        or not server
    ):
        return {
            "type": "session-undescribed",
            "reason": "describe-session needs a session_key and a server",
        }
    match: Optional[_StubConn] = None
    for conn in reversed(list(_LIVE_CONNS)):
        if conn.describe_refusal and _attributed(conn, session_key, server):
            return {"type": "session-undescribed", "reason": conn.describe_refusal}
        if _describes(conn, session_key, server):
            match = conn
            break
    if match is None:
        return {
            "type": "session-undescribed",
            "reason": f"no live, initialized {server!r} stub is attributed to this session",
        }
    caller = match.caller
    return {
        "type": "session-described",
        "session_key": session_key,
        # The channel the stub's own caller carries, so a holder claimed with it
        # presents the same caller without the asker having to know the channel.
        "channel_id": caller.channel_id if caller is not None else "",
        # True when the stub was served a backend of its own (no ``poolable``,
        # or a hazard retreat), so a holder with this Register pins another.
        "private_backend": match.private_backend,
        "register": copy.deepcopy(match.register),
        "initialize": copy.deepcopy(match.captured_init),
    }
