"""Runtime-wide evidence that the host's MCP connect queue is still moving.

KAS connects every MCP server of one process through a single FIFO queue that
holds only a few connects at a time (``MAX_CONCURRENT_CONNECTIONS = 4`` in the
engine's ``connection-lifecycle.ts``). A queued server is reported as
``connecting`` from the moment it is queued, while its own connect timeout
starts only once it holds a slot. Crew's managed servers travel in the
session-level ``mcpServers`` array, the tier the engine merges last, so on an
agent with many servers they wait behind all of them, and behind any other
session's servers queued earlier in the same process.

The managed-readiness barrier (``session_handle_mcp.wait_mcp_ready``) therefore
cannot tell a stuck server from a queued one by its state. What it can read is
whether the queue moved: a status snapshot, for ANY session on the runtime,
that shows more of that session's servers settled than its previous snapshot
did. :class:`McpConnectProgress` counts such rises in :attr:`epoch`; the reader
loop feeds it every status frame before routing the frame, and the barrier
compares epochs across its deadline.

The comparison is with the session's PREVIOUS snapshot, not with the most it
ever settled: a reload or mode switch on the same session id starts its
servers again from none settled, and its rises must count as movement too.
"""

from __future__ import annotations

import hashlib
from typing import Any

from kiro_crew.acp.types import METHOD_KAS_MCP_STATUS, JsonRpcMessage

#: States in which the engine has released a server's connect slot.
SETTLED_STATES = frozenset({"connected", "failed", "disabled"})

#: Sessions remembered at once. Each entry is a 16-byte digest and one int, so
#: a backend-supplied session id of any length costs the same; past the cap the
#: oldest is forgotten, which can only count one extra rise for a session seen
#: again.
_MAX_SESSIONS = 1024


def _key(session_id: str) -> bytes:
    return hashlib.blake2b(session_id.encode("utf-8", "surrogatepass"), digest_size=16).digest()


class McpConnectProgress:
    """Counts rises in any session's settled-server count between its snapshots."""

    def __init__(self) -> None:
        self._last: dict[bytes, int] = {}
        #: Grows by one per rise; never decreases.
        self.epoch = 0

    def observe(self, msg: JsonRpcMessage) -> None:
        """Read one inbound frame; only a ``_kiro/mcp/status`` notification counts."""
        if msg.id is not None or not msg.is_method(METHOD_KAS_MCP_STATUS):
            return
        params: Any = msg.params
        if not isinstance(params, dict):
            return
        session_id = params.get("sessionId")
        servers = params.get("servers")
        if not isinstance(session_id, str) or not session_id or not isinstance(servers, list):
            return
        settled = 0
        for server in servers:
            # Backend data: a status that is not a string (a list, an object) is
            # not a settled state, and must never raise inside the shared reader.
            status = server.get("status") if isinstance(server, dict) else None
            if isinstance(status, str) and status in SETTLED_STATES:
                settled += 1
        key = _key(session_id)
        previous = self._last.pop(key, 0)
        self._last[key] = settled
        if len(self._last) > _MAX_SESSIONS:
            del self._last[next(iter(self._last))]
        if settled > previous:
            self.epoch += 1
