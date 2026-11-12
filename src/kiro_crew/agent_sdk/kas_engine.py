"""What Crew knows about a KAS session's engine, for code outside the ACP layer.

The rules live in :mod:`kiro_crew.acp.kas_engine_features`; the one ACP-layer
read happens when a function here is called, so importing this module costs
only the stdlib.
"""

from __future__ import annotations


def discards_mcp_oauth(provider: object) -> bool:
    """Whether *provider*'s engine is KNOWN to drop a remote MCP server's ``oauth``.

    Known only on Crew's own bundle route, whose engine version Crew installed.
    *provider* carries the version itself, or its ``client`` does.
    """
    from kiro_crew.acp.kas_engine_features import engine_discards_oauth

    version = getattr(provider, "kas_bundle_engine_version", None)
    if not isinstance(version, str):
        version = getattr(getattr(provider, "client", None), "kas_bundle_engine_version", None)
    return engine_discards_oauth(version if isinstance(version, str) else None)
