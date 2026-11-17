"""``class DeepseekAcpClient``: the per-session client for the DeepSeek Harness.

``AcpClient`` is the client every per-session backend shares. What only a DeepSeek
Harness session carries lives here instead: the gate nonce and the READ_ONLY proof its
launch records (:mod:`kiro_crew.acp.harness.deepseek`), its MCP array, the remedy its
gate tripwire names, and the fact that the harness forwards the provider's tool-call
id verbatim, so one id can name two calls of a turn. ``AcpClient`` reaches each through
a hook it declares with the behaviour every other backend keeps, and this class fills
the hook. Building ``AcpClient`` for the deepseek backend builds this class
(``AcpClient.__new__``), so no caller names it.

The members are bound from :mod:`kiro_crew.acp.client_deepseek_session` the way
``AcpClient`` binds its own owners, and the class keeps the facade as its module for
reprs, pickles and type hints.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp import client_deepseek_session as _client_deepseek_session
from kiro_crew.acp.client_class import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_DEEPSEEK


class DeepseekAcpClient(AcpClient, serves=ACP_BACKEND_DEEPSEEK):
    """The ``AcpClient`` of a DeepSeek Harness session."""

    # The facade is the class's import path, so pickles and reprs keep naming it.
    __module__ = "kiro_crew.acp.client"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The nonce the launch issued (``DeepseekLaunch.resolve_spawn``). Non-empty
        # arms the completed-unasked tripwire (``_tripwire_pi_gate``).
        self._deepseek_gate_nonce = ""
        # Why this session's READ_ONLY turns may not approve its read tools by name,
        # or ``""`` once its launch proved stock dsh composes them
        # (``tool_gate.deepseek_read_only_owner_issue``). Refused until then.
        self._deepseek_read_only_issue = "the harness composition was not read back"

    # The deepseek session contract: ``kiro_crew.acp.client_deepseek_session``.
    _deepseek_session_mcp_servers = _client_deepseek_session._deepseek_session_mcp_servers

    # The ``AcpClient`` hooks this class fills, each bound from the deepseek owner.
    _harness_session_mcp_servers = _client_deepseek_session._harness_session_mcp_servers
    #: The remedy the completed-unasked tripwire names for this harness.
    _gate_tripwire_remedy = _client_deepseek_session.DEEPSEEK_GATE_TRIPWIRE_REMEDY
    #: dsh forwards the provider's tool-call id verbatim, so two calls of one turn can
    #: share an id; the harness tool name such an id names is then unknown.
    _tool_call_ids_repeat_in_turn = True
