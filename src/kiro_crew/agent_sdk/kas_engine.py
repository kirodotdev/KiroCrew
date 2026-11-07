"""What a KAS session's engine announced it accepts, for code outside the ACP layer.

The flags and their reading live in :mod:`kiro_crew.acp.kas_engine_features`; the
one ACP-layer read happens when a function here is called, so importing this
module costs only the stdlib.
"""

from __future__ import annotations


def keeps_mcp_oauth(provider: object) -> bool:
    """Whether *provider*'s engine announced it keeps a remote MCP server's ``oauth``."""
    from kiro_crew.acp.kas_engine_features import MCP_SERVER_OAUTH, advertised_by

    return MCP_SERVER_OAUTH in advertised_by(provider)
