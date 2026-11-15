"""``class OpencodeAcpClient``: the per-session client for opencode.

``AcpClient`` is the client every per-session backend shares. What only an opencode
session does lives here instead: holding a shell permission back until the update that
carries its command has reached the security cache, refusing a session left on
opencode's hosted fall-back model before its first prompt, and handing out its MCP
array. ``AcpClient`` reaches each through a hook it declares with the behaviour every
other backend keeps, and this class fills the hook. Building ``AcpClient`` for the
opencode backend builds this class (``AcpClient.__new__``), so no caller names it.

The members are bound from :mod:`kiro_crew.acp.client_opencode_session` the way
``AcpClient`` binds its own owners, and the class keeps the facade as its module for
reprs, pickles and type hints.
"""

from __future__ import annotations

from kiro_crew.acp import client_opencode_session as _client_opencode_session
from kiro_crew.acp.client_class import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_OPENCODE


class OpencodeAcpClient(AcpClient, serves=ACP_BACKEND_OPENCODE):
    """The ``AcpClient`` of an opencode session."""

    # The facade is the class's import path, so pickles and reprs keep naming it.
    __module__ = "kiro_crew.acp.client"

    # The opencode session contract: ``kiro_crew.acp.client_opencode_session``.
    _opencode_session_mcp_servers = _client_opencode_session._opencode_session_mcp_servers

    # The ``AcpClient`` hooks this class fills, each bound from the opencode owner.
    #: A shell permission can arrive ahead of the update that carries its command.
    _defers_shell_permission = True
    _defer_shell_permission = _client_opencode_session._defer_shell_permission
    _refuse_harness_default_model = _client_opencode_session._refuse_harness_default_model
    _harness_session_mcp_servers = _client_opencode_session._harness_session_mcp_servers
