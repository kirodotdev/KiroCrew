"""Wire names of the agent socket, shared by the gateway and its in-sandbox callers.

The agent socket (``docs/request-for-change/rfc-sandbox-ipc-capability.md``,
section 3.3) is a second unix listener that serves only the gateway's internal
routes, and only to a caller holding a capability it redeemed from a one-time
pickup. The gateway side lives in :mod:`kiro_crew.dashboard.agent_ipc`; the
client side in :mod:`kiro_crew.agent_ipc_client`.

A stdlib-only leaf: an in-sandbox MCP server imports it on its startup path, and
the gateway imports it too, so both read one spelling of each name.
"""

from __future__ import annotations

from pathlib import Path

#: Env name of the one-time pickup on a Kiro Crew server's own ``mcpServers``
#: element. It never goes into a harness process environment or onto disk.
PICKUP_ENV = "KIROCREW_IPC_PICKUP"

#: Request header carrying the capability a redemption returned.
CAPABILITY_HEADER = "X-Ipc-Capability"

#: The one route the agent socket serves without a capability.
REDEEM_PATH = "/api/ipc/redeem"

#: Which session a capability acts for right now, answered by the gateway. A server
#: under the credential mask cannot verify the signed token mapping itself, and a
#: warm-pool runtime is re-keyed after its servers start, so it asks.
SESSION_PATH = "/api/ipc/session"

#: Env name of the pid of the gateway that issued the pickup, on the same element.
#: The client refuses to send anything to a listener whose kernel peer pid is not it,
#: so a process that rebinds the socket path receives neither pickup nor request.
GATEWAY_PID_ENV = "KIROCREW_IPC_GATEWAY_PID"

#: Data-home leaf the agent socket is bound in. Readable to every sandbox child on
#: purpose: it holds no credential, and a connection to it grants nothing without a
#: capability bound to the connecting process.
AGENT_IPC_LEAF = "agent-ipc"


def agent_socket_name(port: int) -> str:
    """File name of the agent socket for *port*."""
    return f"agent-{int(port)}.sock"


def agent_socket_path(port: int) -> Path:
    """Path of the agent socket for *port* under this process's data home.

    The ``config`` import is lazy for the reason ``dashboard.urls`` gives for
    ``dashboard_socket_path``: only a caller that uses the socket pays for it.
    """
    from kiro_crew.config.loader import config_dir

    return config_dir() / AGENT_IPC_LEAF / agent_socket_name(port)
