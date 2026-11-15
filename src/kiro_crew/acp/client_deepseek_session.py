"""The DeepSeek Harness session contract ``DeepseekAcpClient`` keeps.

``dsh --profile acp`` reads no ``~/.kiro/agents/<name>.json``, so the session's MCP
array is the only route for Crew's own tools, the spec's servers and a member
session's own mounts. Its launch (:mod:`kiro_crew.acp.harness.deepseek`) composes
Crew's gate plugin into the harness and records the gate nonce and the READ_ONLY
proof on the session; the permission path reads both.

Each function here whose first parameter is ``self`` is a ``DeepseekAcpClient`` method:
the class binds it by name, so it reads and writes the session's own state on the
instance, and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: none.

Instance fields read: none.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.acp.client import DeepseekAcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")

#: What the chat tells the person when a DeepSeek Harness tool call completed that
#: Crew's gate was never asked about (``client_permissions._tripwire_pi_gate``).
DEEPSEEK_GATE_TRIPWIRE_REMEDY = (
    "Start a new chat, and if it recurs check the harness version: its "
    "launcher must keep applying the --patch overlay that composes the "
    "gate plugin, and its tools core must keep resolving an `ask` through "
    "its approval service."
)


def _deepseek_session_mcp_servers(self: DeepseekAcpClient) -> list:
    """MCP server array passed to a deepseek ``session/new`` / ``session/resume``.

    The deepseek twin of :meth:`_goose_session_mcp_servers`: ``dsh --profile acp``
    reads no ``~/.kiro/agents/<name>.json``, and with deepseek in ``MIRRORS``
    :meth:`_pooled_mcp_servers` answers ``[]`` for it, so this array is the only
    route for Crew's own tools, the spec's servers and a member session's own
    mounts (session control and the panel). An element whose command cannot start
    fails ``session/new`` whole on this harness
    (``test/fixtures/acp_frames/deepseek/mcp-stdio-rollback-live.jsonl``).

    In-memory only. The spawn path warms ``_session_mcp_cache`` off the loop, so
    this accessor adds no scheduling or failure point to a call site shared with
    kiro-cli (harness-parity H13).
    """
    return self._session_mcp_servers()


# The ``AcpClient`` hooks ``DeepseekAcpClient`` fills, each the deepseek step above.


def _harness_session_mcp_servers(self: DeepseekAcpClient) -> list:
    """The servers a deepseek session's MCP array carries ahead of the shared entries."""
    return self._deepseek_session_mcp_servers()
