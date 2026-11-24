"""The consent URL of a mid-session MCP sign-in, and the notification that ends it.

When a connected server answers 401, KAS sends ``_kiro/openExternalUrl`` by itself, with
``serverName``, ``oauthCompletion: 'notification'`` and an ``oauthFlowId``, and later one
``_kiro/mcp/oauthCompleted`` carrying the same id. Neither names a session, and two sessions
on one runtime can hold a server of the same name (possibly for two different people), so the
link goes to a session only when exactly one live session on the runtime holds that server.
Otherwise it is refused as before: a link handed to the wrong session would bind that
person's sign-in to someone else's connection.

Each function here whose first parameter is ``self`` takes an ``AcpRuntime``.

Instance fields written: ``_mcp_lazy_sign_in_flows``.

Instance fields read: ``_fold_mcp_frame``, ``_mcp_reports``, ``_session_queues``.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew.acp.mcp_session_report import NAME_CAP, sanitize_sink_text
from kiro_crew.acp.types import (
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    JsonRpcMessage,
)

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

logger = logging.getLogger("kiro_crew.acp.runtime")

#: How many mid-session sign-ins a runtime remembers. The engine waits at most five
#: minutes per server connection, so this only bounds flows whose end never came.
_FLOW_CAP = 32
#: Longest ``oauthFlowId`` kept; a longer one is treated as absent.
_FLOW_ID_CAP = 200
#: The ``error`` of the init-failure frame a failed mid-session sign-in becomes.
_FAILED_SIGN_IN = "sign-in did not finish"


class LazySignInFlow(NamedTuple):
    """The session a mid-session sign-in's link went to, and that link."""

    session_id: str
    server_name: str
    url: str


#: A runtime's open mid-session sign-ins, oldest first, by flow key.
LazySignInFlows = OrderedDict[str, LazySignInFlow]


def is_mid_session_consent(params: Any) -> bool:
    """Whether a ``_kiro/openExternalUrl`` request is a mid-session sign-in's link."""
    return (
        isinstance(params, dict)
        and isinstance(params.get("serverName"), str)
        and params.get("oauthCompletion") == "notification"
    )


def _flows(self: AcpRuntime) -> LazySignInFlows:
    """The runtime's open mid-session sign-ins, created on first use.

    Read through this rather than the attribute, as ``_mcp_reports`` is: a runtime
    built with ``object.__new__`` never ran ``__init__``.
    """
    flows: LazySignInFlows | None = getattr(self, "_mcp_lazy_sign_in_flows", None)
    if flows is None:
        flows = OrderedDict()
        self._mcp_lazy_sign_in_flows = flows
    return flows


def _flow_id(params: dict[str, Any]) -> str:
    raw = params.get("oauthFlowId")
    return raw if isinstance(raw, str) and 0 < len(raw) <= _FLOW_ID_CAP else ""


def lone_holder(self: AcpRuntime, server_name: str) -> tuple[str, str]:
    """The one live session holding *server_name*, or ``""`` and why there is none.

    A session holds a server it put on the wire or the host reported for it. A
    session whose report is missing or partial might hold it unseen, so it counts
    as a second holder: the answer fails closed.
    """
    clean = sanitize_sink_text(server_name, NAME_CAP)
    if not clean or clean != server_name:
        return "", "the MCP server name is not a plain name"
    reports = self._mcp_reports()
    holders: list[str] = []
    unknown = False
    for session_id in list(self._session_queues):
        report = reports.get(session_id)
        if report is None or report.left_out:
            unknown = True
        elif clean in report.configured or report.has_reported(clean):
            holders.append(session_id)
    if len(holders) == 1 and not unknown:
        return holders[0], ""
    if holders or unknown:
        return "", "more than one session may hold that MCP server"
    return "", "no session holds that MCP server"


async def deliver_mid_session_consent(self: AcpRuntime, params: dict[str, Any], url: str) -> str:
    """Hand a mid-session sign-in's link to the one session holding its server.

    Returns the refusal, or ``""`` once the link is on that session's queue or is a
    repeat of the link already shown for the same flow. A new link for a known flow
    is delivered: the engine made a new code verifier for it, so only the newest
    link can finish the sign-in, and the chat retires the older one when it draws
    this one.
    """
    server_name: str = params["serverName"]
    session_id, refusal = lone_holder(self, server_name)
    queue = self._session_queues.get(session_id) if session_id else None
    if queue is None:
        logger.info(
            "Mid-session MCP sign-in link for %s refused: %s",
            sanitize_sink_text(server_name, NAME_CAP),
            refusal or "its session has left",
        )
        return refusal or "the MCP sign-in's session has left"
    flows = _flows(self)
    # A link without an id is keyed by its owner, which no engine id can equal.
    key = _flow_id(params) or f"\0{session_id}\0{server_name}"
    flow = LazySignInFlow(session_id, server_name, url)
    if flows.get(key) == flow:
        return ""
    flows[key] = flow
    flows.move_to_end(key)
    while len(flows) > _FLOW_CAP:
        flows.popitem(last=False)
    consent = JsonRpcMessage(
        method=METHOD_MCP_OAUTH_REQUEST,
        params={"sessionId": session_id, "serverName": server_name, "oauthUrl": url},
    )
    # Made here, so the reader never saw it: folded as the reader folds one.
    self._fold_mcp_frame(session_id, consent)
    await queue.put(consent)
    return ""


async def settle_mid_session_sign_in(self: AcpRuntime, params: Any) -> None:
    """Turn ``_kiro/mcp/oauthCompleted`` into the end of the sign-in its session was shown.

    Matched by ``oauthFlowId``; by server name only when the notification has no id
    and exactly one open flow names that server. It becomes an ordinary
    ``server_initialized`` or ``server_init_failure`` frame on that session's queue,
    which closes the chat's sign-in card. A success is folded into the session's MCP
    report (the server is up again); a failure is not, so the server keeps reading as
    waiting for a sign-in. A notification that matches no flow is dropped.
    """
    if not isinstance(params, dict):
        return
    server_name = params.get("serverName")
    flows = _flows(self)
    flow_id = _flow_id(params)
    if flow_id:
        flow = flows.pop(flow_id, None)
    else:
        named = [key for key, item in flows.items() if item.server_name == server_name]
        flow = flows.pop(named[0]) if len(named) == 1 else None
    queue = self._session_queues.get(flow.session_id) if flow is not None else None
    if flow is None or queue is None:
        logger.debug("MCP sign-in end matched no open sign-in")
        return
    frame_params = {"sessionId": flow.session_id, "serverName": flow.server_name}
    if params.get("success") is True:
        frame = JsonRpcMessage(method=METHOD_MCP_SERVER_INITIALIZED, params=frame_params)
        self._fold_mcp_frame(flow.session_id, frame)
    else:
        frame = JsonRpcMessage(
            method=METHOD_MCP_SERVER_INIT_FAILURE,
            params={**frame_params, "error": _FAILED_SIGN_IN},
        )
    await queue.put(frame)
