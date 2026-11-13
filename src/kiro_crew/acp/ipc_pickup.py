"""Put an agent-socket pickup on ``kirocrew-core``'s element for an enforced harness.

An enforced harness (``tool_gate.is_enforced``: codex, opencode, goose, pi) runs
under an OS credential mask that hides the gateway's internal secret from its whole
process tree, so the ``kirocrew-core`` it launches has no credential and every
gateway-backed tool is refused. The gateway's agent socket
(:mod:`kiro_crew.dashboard.agent_ipc`) admits that server by a capability instead,
redeemed from a one-time pickup that rides the server's own element ``env``.

The runtime that builds a session's array calls this with what only it holds: the
backend, the session's stub token, a reader of its own root pid, and itself as the
owner whose lifetime the pickup shares. The element also carries this gateway's pid,
which the server checks against the kernel peer pid of the listener before it sends
anything. Every other harness keeps its array byte for byte: it reads the secret, so
it needs no pickup.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from kiro_crew.agent_ipc_wire import BRIDGE_PICKUP_PREFIX, GATEWAY_PID_ENV, PICKUP_ENV
from kiro_crew.agent_sdk.tool_gate import is_enforced
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.mcp_gateway.hashing import expand_stub_flags
from kiro_crew.providers.mirrors.identity import with_env

logger = logging.getLogger(__name__)

#: The Kiro Crew server that redeems a pickup at startup (``agent_ipc_client``).
PICKUP_SERVERS: frozenset[str] = frozenset({"kirocrew-core"})


def _launches_server_directly(element: dict[str, Any]) -> bool:
    """Whether *element* starts the named server itself rather than the broker stub.

    A broker stub (``-m`` :data:`~kiro_crew.mcp_gateway.STUB_MODULE`) never takes the
    pickup out of its environment, so a same-uid process in the session could read
    it from the stub's ``/proc/<pid>/environ`` and redeem it. An argv that cannot be
    read answers ``False``: no pickup is the behaviour without an agent socket.
    """
    args = element.get("args") or []
    if not isinstance(args, list):
        return False
    try:
        argv = expand_stub_flags(args)
    except ValueError:
        return False
    return not any(
        token == "-m" and i + 1 < len(argv) and argv[i + 1] == STUB_MODULE
        for i, token in enumerate(argv)
    )


def _is_connection_bridge(element: Any) -> bool:
    """Whether *element* starts a registered Crew-owned Connection bridge.

    The bridge (``kas_connection_bridge``) reaches the gateway's Connection proxy,
    an internal route, so under the mask it needs the same pickup ``kirocrew-core``
    gets. It takes the pickup out of its environment at startup.
    """
    from kiro_crew.acp.kas_env_launch import is_crew_bridge_launch

    return is_crew_bridge_launch(element)


def with_ipc_pickups(
    servers: list[dict[str, Any]],
    *,
    backend: str,
    session_token: str,
    runtime_pid: Callable[[], int | None],
    owner: object,
) -> list[dict[str, Any]]:
    """*servers* with a fresh pickup on each element :data:`PICKUP_SERVERS` names.

    Unchanged (the same list) unless *backend* is enforced, *session_token* is set,
    and this gateway process serves an agent socket the pickup could be redeemed on.
    Fail-soft: any failure leaves the element as it was, which is today's behaviour.
    """
    if not is_enforced(backend or "") or not session_token:
        return servers
    from kiro_crew.dashboard.agent_ipc import agent_socket_bound, issue_pickup

    if not agent_socket_bound():
        return servers
    out: list[dict[str, Any]] = []
    for element in servers:
        name = element.get("name") if isinstance(element, dict) else None
        bridge = _is_connection_bridge(element)
        if bridge or (name in PICKUP_SERVERS and _launches_server_directly(element)):
            try:
                pickup = issue_pickup(
                    # A bridge's capability serves the Connection proxy route only.
                    server=f"{BRIDGE_PICKUP_PREFIX}{name}" if bridge else str(name),
                    session_token=session_token,
                    runtime_pid=runtime_pid,
                    owner=owner,
                )
                element = with_env(element, {PICKUP_ENV: pickup, GATEWAY_PID_ENV: str(os.getpid())})
            except Exception:
                logger.warning("could not issue an agent-socket pickup for %s", name, exc_info=True)
        out.append(element)
    return out
