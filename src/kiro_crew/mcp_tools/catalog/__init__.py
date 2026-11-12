"""Tool declarations of the ``kirocrew-core`` server, one module per domain.

What ``tools/list`` advertises: each module's ``schemas()`` (and ``advertised()`` where a
domain's tools can be switched off) holds the descriptors of the domain of the same name
under :mod:`kiro_crew.mcp_tools`, whose module holds the handlers.

Nothing in this package imports a tool handler, the config loader, or anything else a
tool call needs and a listing does not. An agent engine starts every MCP server of a
session at session start and holds the first prompt until each has listed its tools, so
what these modules import is paid on every session start
(``kiro_crew.mcp_shared_listing``); ``test_mcp_listing_import_budget`` holds it.

A name a test patches stays patchable on the module it was patched on before the
declarations moved here: a domain's handler module, or ``kiro_crew.mcp_core`` for the
server's plumbing. Importing either loads the handlers, so a builder here reads such a
name with :func:`loaded_binding`, at call time.
"""

from __future__ import annotations

import sys
from typing import Any


def loaded_binding(module: str, name: str, owner: Any) -> Any:
    """``<module>.<name>`` once *module* is loaded, else *owner*, the object it binds.

    Before a server has loaded its handlers, the module that defines the name answers.
    Afterwards the two are one object unless a test has patched the name on *module*,
    which is then the binding to honour.
    """
    return getattr(sys.modules.get(module), name, owner)
