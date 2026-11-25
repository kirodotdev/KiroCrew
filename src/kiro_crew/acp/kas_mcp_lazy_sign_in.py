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

Instance fields read: ``_announced_children``, ``_fold_mcp_frame``, ``_mcp_reports``,
``_mcp_sign_in``, ``_session_queues``, ``session_tenancy``.
"""

from __future__ import annotations

import asyncio
import hashlib
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


#: Engine -> client notification ending a mid-session sign-in: the one whose link
#: came with ``oauthCompletion: 'notification'``. ``params`` carries ``serverName``,
#: ``success`` and the link's ``oauthFlowId``; it names no session.
METHOD_KAS_MCP_OAUTH_COMPLETED = "_kiro/mcp/oauthCompleted"

#: Set on a consent frame whose link replaces the one its session was shown for the
#: same flow; the session handle then passes it on (``_accept_oauth_request``).
REPLACES_LINK = "replacesLink"


class LazySignInFlow(NamedTuple):
    """The session a mid-session sign-in's link went to, and that link."""

    session_id: str
    server_name: str
    #: The link's sha256, for the repeat check: the link itself is external
    #: content of any length, so the flow keeps only a fixed-size digest of it.
    url_digest: str
    #: The queue the link was put on. A completion goes only to this queue, so a
    #: session that left, or a new queue registered under its id, gets nothing.
    queue: asyncio.Queue[Any]
    #: Whether the engine gave the flow an id; a completion without one matches
    #: only flows without one.
    has_id: bool


#: A runtime's open mid-session sign-ins, oldest first, by flow key.
LazySignInFlows = OrderedDict[str, LazySignInFlow]


def flow_open(self: AcpRuntime, session_id: str, server_name: str) -> bool:
    """Whether this session was shown a mid-session sign-in link for *server_name* still open.

    Open until its ``_kiro/mcp/oauthCompleted`` arrives (the engine sends one at the
    latest when its five-minute wait ends), the flow is dropped past the cap, or the
    session's queue goes.
    """
    queue = self._session_queues.get(session_id)
    return queue is not None and any(
        flow.queue is queue and flow.server_name == server_name for flow in _flows(self).values()
    )


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

    The holder is the one session whose MCP report has a host frame for the server
    (``McpSessionReport.server_claim``). Every other session the engine may run
    must be shown not to hold it, so anything that might hold it unseen fails the
    answer closed: an engine session with no queue here (a start still running, an
    unconfirmed teardown, a native subagent), a child session announced to a
    different owner, another
    session whose report is missing, partial, not yet heard from the host, or names
    the server only from Crew's own records, and a name the report would have cut
    or rewritten (it is compared as the report stores it).
    """
    clean = sanitize_sink_text(server_name, NAME_CAP)
    if not clean or clean != server_name or len(clean) >= NAME_CAP:
        return "", "the MCP server name is not a plain name"
    reports = self._mcp_reports()
    holders: list[str] = []
    unknown = False
    for session_id in list(self._session_queues):
        report = reports.get(session_id)
        claim = report.server_claim(clean) if report is not None else "maybe"
        if claim == "held":
            holders.append(session_id)
        elif claim == "maybe":
            unknown = True
    if len(holders) != 1 or unknown:
        if holders or unknown:
            return "", "more than one session may hold that MCP server"
        return "", "no session holds that MCP server"
    # The same unseen tenants process cleanup refuses on: a start still running, a
    # timed-out start collector, a teardown the host never confirmed, a native
    # subagent roster (``runtime_liveness.session_tenancy``).
    if self.session_tenancy(holders[0]) is None:
        return "", "a session this runtime cannot see may hold that MCP server"
    if set(self._announced_children.values()) - {holders[0]}:
        return "", "another session's child session may hold that MCP server"
    return holders[0], ""


def _slot_session(self: AcpRuntime, server_name: str) -> str:
    """The session of an explicit sign-in of *server_name* now in flight, or ``""``.

    The engine can open a second link for that server while the explicit attempt
    runs, and tag it ``notification`` once the handshake is done, so such a link
    may be that attempt's rather than the holder's.
    """
    entry = self._mcp_sign_in
    return entry[0] if entry is not None and entry[1] == server_name else ""


async def deliver_mid_session_consent(self: AcpRuntime, params: dict[str, Any], url: str) -> str:
    """Hand a mid-session sign-in's link to the one session holding its server.

    Returns the refusal, or ``""`` once the link is on that session's queue or is a
    repeat of the link already shown for the same flow. A new link for a known flow
    is delivered marked :data:`REPLACES_LINK`: the engine made a new code verifier
    for it, so only the newest link can finish the sign-in, and the chat retires the
    older one when it draws this one. While an explicit sign-in of the same server
    holds the slot for a session other than the holder, the link is refused.
    """
    server_name: str = params["serverName"]
    session_id, refusal = lone_holder(self, server_name)
    if session_id and _slot_session(self, server_name) not in ("", session_id):
        session_id, refusal = "", "more than one session may hold that MCP server"
    queue = self._session_queues.get(session_id) if session_id else None
    if queue is None:
        logger.info(
            "Mid-session MCP sign-in link for %s refused: %s",
            sanitize_sink_text(server_name, NAME_CAP),
            refusal or "its session has left",
        )
        return refusal or "the MCP sign-in's session has left"
    flows = _flows(self)
    flow_id = _flow_id(params)
    # A link without an id is keyed by its owner, which no engine id can equal.
    key = flow_id or f"\0{session_id}\0{server_name}"
    digest = hashlib.sha256(url.encode("utf-8", "surrogatepass")).hexdigest()
    flow = LazySignInFlow(session_id, server_name, digest, queue, bool(flow_id))
    known = flows.get(key)
    if known == flow:
        return ""
    flows[key] = flow
    flows.move_to_end(key)
    while len(flows) > _FLOW_CAP:
        _, dropped = flows.popitem(last=False)
        logger.info(
            "Mid-session MCP sign-in for %s forgotten past %d open ones: its card "
            "will not close by itself",
            sanitize_sink_text(dropped.server_name, NAME_CAP),
            _FLOW_CAP,
        )
    consent_params: dict[str, Any] = {
        "sessionId": session_id,
        "serverName": server_name,
        "oauthUrl": url,
    }
    if known is not None and known.queue is queue:
        # The handle shows one link per server until the sign-in ends; this one
        # replaces the link it showed, so it must not be dropped as a repeat.
        consent_params[REPLACES_LINK] = True
    consent = JsonRpcMessage(method=METHOD_MCP_OAUTH_REQUEST, params=consent_params)
    # Made here, so the reader never saw it: folded as the reader folds one.
    self._fold_mcp_frame(session_id, consent)
    await queue.put(consent)
    return ""


async def settle_mid_session_sign_in(self: AcpRuntime, params: Any) -> None:
    """Turn ``_kiro/mcp/oauthCompleted`` into the end of the sign-in its session was shown.

    Matched by ``oauthFlowId``; by server name only when the notification has no id
    and exactly one open flow without an id names that server. It reaches only the
    queue the link went to: a session that left, or one registered since under the
    same id, gets nothing. It becomes an ordinary
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
        named = [
            key
            for key, item in flows.items()
            if not item.has_id and item.server_name == server_name
        ]
        flow = flows.pop(named[0]) if len(named) == 1 else None
    queue = self._session_queues.get(flow.session_id) if flow is not None else None
    if flow is None or queue is None or queue is not flow.queue:
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
