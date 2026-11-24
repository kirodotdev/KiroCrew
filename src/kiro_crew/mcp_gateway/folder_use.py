"""Facts that answer "does this server work on the chat's project folder?".

A shared server starts in one fixed folder for every chat, so MCP Management asks
before it shares a server that is not cleared. It skips the question when a
declared or observed fact answers it (spec: ``docs/system-specs/modules/
mcp-shareability.md``, "The working-folder question gates routing"):

1. The gateway saw the server send ``roots/list`` (``HAZARD_ROOTS_REQUESTED``):
   it reads the chat's folder, whatever was answered. It stays in each chat, its
   Share switch is refused, and it is not asked.
2. Kiro Crew's own servers do not read the folder.
3. A server the edition lists (``McpToolingProvider.folder_free_mcp_servers``).

Every other server is asked, and its answer is kept in ``mcp_gateway.cwd_use``.
Config hints such as a ``.`` argument are not read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.mcp_gateway import hazards

#: Where a "does not use the folder" fact came from (``folder_free_source``).
SOURCE_CREW = "crew"
SOURCE_EDITION = "edition"


def edition_folder_free() -> frozenset[str]:
    """Server names the running edition declares do not use the project folder.

    Read through ``getattr`` so an edition provider that predates the method keeps
    working and declares none. Non-string members are dropped.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    def _read() -> frozenset[str]:
        reader = getattr(current_context().mcp_tooling, "folder_free_mcp_servers", None)
        names = reader() if callable(reader) else ()
        return frozenset(n for n in names if isinstance(n, str) and n)

    return safe_context_call(
        _read,
        fallback_factory=frozenset,
        log_message="folder_free_mcp_servers lookup failed; declaring no server folder-free",
    )


def folder_free_source(name: str, edition: frozenset[str] | None = None) -> str | None:
    """Which declared fact says *name* does not use the folder, or None."""
    if name in KIROCREW_BIN_MCP_SERVERS:
        return SOURCE_CREW
    if name in (edition_folder_free() if edition is None else edition):
        return SOURCE_EDITION
    return None


def asks_for_roots(observed: Mapping[str, tuple[str, ...]], name: str) -> bool:
    """Whether the hazard ledger *observed* holds a ``roots/list`` request by *name*."""
    return hazards.HAZARD_ROOTS_REQUESTED in observed.get(name, ())


def roots_requesting(socket_path: str | Path = "") -> frozenset[str]:
    """Every server the gateway saw ask for roots. BLOCKING file read.

    Identity-blind on purpose: an upgrade does not make a server that asked for
    the chat's folder stop working on it, and a wrong answer here only keeps a
    server in each chat. A missing or unreadable ledger reads as none.
    """
    from kiro_crew.mcp_gateway.rewriter_overlay import records_dir

    try:
        observed = hazards.load_ledger(records_dir(socket_path)).as_dict()
    except OSError:
        return frozenset()
    return frozenset(name for name in observed if asks_for_roots(observed, name))
