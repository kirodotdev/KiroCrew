"""The tool catalogue of the ``kirocrew-core`` MCP server, and the server's declaration.

What ``tools/list`` returns and what ``initialize`` says about the server. Nothing here
imports a tool handler: an agent engine holds a session's first prompt until every MCP
server has listed its tools, so this is all the server loads before it answers
(``kiro_crew.mcp_shared_listing``). The descriptors are declared per domain under
:mod:`kiro_crew.mcp_tools.catalog`; the handlers stay in ``kiro_crew.mcp_core`` and
:mod:`kiro_crew.mcp_tools`, and ``kiro_crew.mcp_core`` reads every name here as its own.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.mcp_shared_listing import StdioServer
from kiro_crew.mcp_tool_titles import with_titles
from kiro_crew.mcp_tools import build_tool_list, build_tool_names
from kiro_crew.session_directive import CORE_MCP_SERVER


def _list_tools() -> list[dict[str, Any]]:
    """Tool descriptors served for ``tools/list``.

    Declared per domain under :mod:`kiro_crew.mcp_tools.catalog`; this stays the
    entry point kiro-cli and in-process discovery both read.
    """
    return with_titles(CORE_MCP_SERVER, build_tool_list())


def _list_tool_names() -> list[str]:
    """Tool NAMES only, without assembling descriptions.

    The names-only read path ``mcp_discovery._managed_tools_in_process`` prefers
    when it keeps only names. It returns exactly the names ``_list_tools`` would,
    in the same order, but ``build_tool_names`` takes the ``names_only`` path so
    the two descriptions that reach for a live value (the sub-agent cap and
    agents-directory scan in ``mcp_tools.spawn``, the config reads in
    ``mcp_tools.control``) are never performed. ``with_titles`` is intentionally
    not applied: it only decorates descriptions, which a names-only caller
    discards.
    """
    return build_tool_names()


def _redeem_pickup() -> None:
    """Redeem an enforced harness's one-time pickup, when this process carries one.

    Such a harness hands the server a pickup in place of the secret it withholds
    (``kiro_crew.agent_ipc_client``). It has to be redeemed before the first request,
    because listing the tools already reads this session's tool policy from the
    gateway. A second call finds no pickup and does nothing.
    """
    from kiro_crew.agent_ipc_client import redeem_at_startup
    from kiro_crew.port_resolution import resolve_client_port_src

    redeem_at_startup(lambda: resolve_client_port_src(None)[0])


#: The server as ``initialize`` describes it. ``kiro_crew.mcp_core.run_mcp_core_server``
#: passes the loop these same values, so the answers written before the handlers load
#: and the ones the loop writes after cannot differ.
#:
#: ``advertise_caller_identity``: kirocrew-core consumes the per-call
#: ``kirocrew.caller`` identity, so it is safe to share one backend across sessions.
#: What advertising buys is the injected block, not co-tenancy: a backend that does
#: NOT advertise is pooled all the same (nothing declines to pool one; see
#: ``rewriter.UNPOOLABLE_SERVERS``) and simply never receives an identity.
#: ``reports_inflight``: the only server that parks ``DeferredTool`` calls, so the
#: only one whose gateway needs the in-flight report.
SERVER = StdioServer(
    name=CORE_MCP_SERVER,
    version="1.0.0",
    list_tools=_list_tools,
    advertise_caller_identity=True,
    reports_inflight=True,
    before_first_request=_redeem_pickup,
)
