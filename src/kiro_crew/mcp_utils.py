"""Pure helpers for MCP server-key handling, shared without circular imports.

Lives in its own module (rather than ``agent.py``) so both ``agent.py`` and
``dashboard/handlers/mcp.py`` can import it at the top level -- ``agent`` imports
the handlers module, so a helper defined in ``agent`` could only be reached from
the handlers via an in-function import.  Keeping it dependency-free here removes
that workaround.
"""

from __future__ import annotations

import re


def mcp_server_alias(name: str) -> str:
    """Return a kiro-safe (slash-free) alias for an MCP server key.

    kiro-cli resolves agent ``tools``/``allowedTools`` entries of the form
    ``@server`` by splitting on ``/`` (``@server/tool``).  A server key that
    contains ``/`` -- e.g. the npm-scoped ``npm:@playwright/mcp`` or the MCP
    registry ``namespace/name`` form -- can therefore never be referenced as
    ``@key``: kiro reads the trailing path segment as a (non-existent) tool
    name and exposes none of the server's tools.

    Map such keys to a stable, descriptive, slash-free slug
    (``npm:@playwright/mcp`` -> ``playwright-mcp``).  Slash-free names are
    returned unchanged so existing well-formed configs are untouched.
    """
    if not name or "/" not in name:
        return name
    slug = name.split(":", 1)[1] if ":" in name else name
    slug = slug.lstrip("@").replace("/", "-").replace("@", "-")
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", slug).strip("-")
    return slug or "mcp-server"
