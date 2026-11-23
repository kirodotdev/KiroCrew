"""What Crew knows about a KAS session's engine, for code outside the ACP layer.

The rules live in :mod:`kiro_crew.acp.kas_engine_features`; the one ACP-layer
read happens when a function here is called, so importing this module costs
only the stdlib.
"""

from __future__ import annotations


def _field(provider: object, name: str) -> str:
    """*provider*'s str field *name*, or its ``client``'s, else ``""``."""
    value = getattr(provider, name, None)
    if not isinstance(value, str):
        value = getattr(getattr(provider, "client", None), name, None)
    return value if isinstance(value, str) else ""


def discards_mcp_oauth(provider: object) -> bool:
    """Whether *provider*'s engine is KNOWN to drop a remote MCP server's ``oauth``.

    Known on Crew's own bundle route from the engine version Crew installed, and
    on the relay from the engine kiro-cli unpacked, read at spawn. The bundle
    version wins when set. *provider* carries the versions itself, or its
    ``client`` does.
    """
    from kiro_crew.acp.kas_engine_features import engine_discards_oauth

    engine = _field(provider, "kas_bundle_engine_version") or _field(
        provider, "kas_relay_engine_version"
    )
    return engine_discards_oauth(engine)
