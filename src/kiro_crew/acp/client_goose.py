"""``class GooseAcpClient``: the per-session client for goose.

``AcpClient`` is the client every per-session backend shares. What only a goose session
does lives here instead: reading its resolved permission mode back off the answers that
open and restore the session, stopping a session that reports leaving that mode, naming
a goose release outside the verified range, and handing out its MCP array. ``AcpClient``
reaches each through a hook it declares with the behaviour every other backend keeps,
and this class fills the hook. Building ``AcpClient`` for the goose backend builds this
class (``AcpClient.__new__``), so no caller names it.

The members are bound from :mod:`kiro_crew.acp.client_goose_session` the way
``AcpClient`` binds its own owners, and the class keeps the facade as its module for
reprs, pickles and type hints.
"""

from __future__ import annotations

from kiro_crew.acp import client_goose_session as _client_goose_session
from kiro_crew.acp.client_class import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_GOOSE


class GooseAcpClient(AcpClient, serves=ACP_BACKEND_GOOSE):
    """The ``AcpClient`` of a goose session."""

    # The facade is the class's import path, so pickles and reprs keep naming it.
    __module__ = "kiro_crew.acp.client"

    # The goose session contract: ``kiro_crew.acp.client_goose_session``.
    _verify_goose_routing = _client_goose_session._verify_goose_routing
    _note_goose_version = _client_goose_session._note_goose_version
    _tripwire_goose_mode = _client_goose_session._tripwire_goose_mode
    _goose_session_mcp_servers = _client_goose_session._goose_session_mcp_servers

    # The ``AcpClient`` hooks this class fills, each bound from the goose owner.
    #: This backend's session answers carry the mode Crew must read back.
    _reads_back_session_mode = True
    _harness_session_mcp_servers = _client_goose_session._harness_session_mcp_servers
    _verify_session_mode = _client_goose_session._verify_session_mode
    _tripwire_session_mode = _client_goose_session._tripwire_session_mode
    _note_harness_adapter_version = _client_goose_session._note_harness_adapter_version
