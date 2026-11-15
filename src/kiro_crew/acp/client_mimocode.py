"""``class MimocodeAcpClient``: the per-session client for MiMo Code.

``AcpClient`` is the client every per-session backend shares. What only a MiMo Code
session does lives here instead: refusing a session left on Xiaomi's hosted model with
no sign-in before its first prompt. ``AcpClient`` reaches it through a hook it declares
with the behaviour every other backend keeps, and this class fills the hook. Building
``AcpClient`` for the mimocode backend builds this class (``AcpClient.__new__``), so no
caller names it.

The members are bound from :mod:`kiro_crew.acp.client_mimocode_session` the way
``AcpClient`` binds its own owners, and the class keeps the facade as its module for
reprs, pickles and type hints.
"""

from __future__ import annotations

from kiro_crew.acp import client_mimocode_session as _client_mimocode_session
from kiro_crew.acp.client_class import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_MIMOCODE


class MimocodeAcpClient(AcpClient, serves=ACP_BACKEND_MIMOCODE):
    """The ``AcpClient`` of a MiMo Code session."""

    # The facade is the class's import path, so pickles and reprs keep naming it.
    __module__ = "kiro_crew.acp.client"

    # The ``AcpClient`` hook this class fills, bound from the MiMo Code owner.
    _refuse_harness_default_model = _client_mimocode_session._refuse_harness_default_model
