"""A managed MCP server's first two answers, built by the gateway for the session asking.

``GET /api/session-tool-policy`` grows one optional job: a server that names itself in the
``listing`` parameter gets, beside its policy, its ``initialize`` result and its filtered
tool list, which it relays to its client before it has loaded its own configuration and
platform (``kiro_crew.mcp_listing_relay``, where the reason and the server side live).

Both answers come from the functions the server itself would have called: its declared
``list_tools``, ``mcp_shared_listing.within_policy`` and ``initialize_result``. So the
gateway answers with a list only where it is the list that server would have built, and
with a reason (``{"local": ...}``) everywhere else, which leaves the server on the path it
has always taken. Nothing is kept between requests: each list is built for the request
that asked, in a worker thread, under that session as the request-local caller.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from typing import Any, Mapping

from kiro_crew import mcp_listing_relay
from kiro_crew.mcp_caller import CallerContext, current_caller, set_current_caller
from kiro_crew.mcp_entry import EARLY_ANSWERS
from kiro_crew.mcp_shared_listing import StdioServer, initialize_result, within_policy
from kiro_crew.mcp_shared_policy import ToolPolicy, _parse_policy_include
from kiro_crew.platform import PROFILE_STANDALONE, current_context
from kiro_crew.platform.governance_profiles import resolve_active_scope
from kiro_crew.sandbox_tiers import _gateway_package_dir, install_root_targets_for

logger = logging.getLogger(__name__)

#: The listing code this process found on disk as it started to serve, which is the code
#: it runs, or ``""`` until :func:`startup_stamp`'s worker has read it. A server that
#: finds other code there (a package replaced since) builds its own list, and so does
#: every server that asks before the stamp is in.
#: Read once, at start, and not at the first request: a stamp taken later vouches for
#: what is on disk by then, which after an upgrade is not the code this process loaded,
#: and the gateway would relay the old list as the new server's own.
_CODE_AT_START = ""
_STAMP_TASKS: set[asyncio.Future[None]] = set()


def note_code_at_start() -> None:
    """Read the listing code stamp this process answers by. Blocking: a stat for each
    listing source and three directory listings, a cost the install fixes."""
    global _CODE_AT_START
    _CODE_AT_START = mcp_listing_relay.code_stamp()


async def startup_stamp(_app: Any = None) -> None:
    """The gateway's startup hook: start :func:`note_code_at_start` in a worker thread
    and return.

    Nothing is awaited in front of the listener (``no-new-work-on-gateway-boot-path``),
    and nothing is read when this module is imported. A server that asks before the
    worker is done is answered "build locally", which is the start it always had.
    """
    task = asyncio.ensure_future(asyncio.to_thread(note_code_at_start))
    _STAMP_TASKS.add(task)
    task.add_done_callback(_stamp_done)


def _stamp_done(task: asyncio.Future[None]) -> None:
    """Drop the finished read; a failure leaves no stamp, so no list is relayed."""
    _STAMP_TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("the listing code stamp could not be read", exc_info=task.exception())


def _package_tree_is_sealed() -> bool:
    """Whether the sandbox seals the tree this gateway imports from against a session's agent.

    The sandbox's own classification and nothing beside it
    (``sandbox_tiers.install_root_targets_for``): a packaged install is sealed read-only
    for every sandboxed child; an editable or from-source checkout is the developer's
    working copy and stays writable. Building a list imports listing modules into this
    process, outside the sandbox, on a request a session can cause, so it is done only
    where a session's agent cannot have put the code there. No answer is not a yes.
    """
    try:
        return bool(install_root_targets_for(_gateway_package_dir()))
    except Exception:  # noqa: BLE001 - a classification that cannot be had is "not sealed"
        return False


def _policy_of(answer: Mapping[str, Any] | None) -> ToolPolicy | None:
    """The policy *answer* states, as the server reads it, or ``None`` when it cannot.

    The server refuses calls on an ``exclude`` or ``include`` of the wrong shape and
    lists everything; it reaches that verdict itself, so no list is built for it here.
    """
    if answer is None:
        return ToolPolicy(frozenset(), "")
    exclude = answer.get("exclude", [])
    if not isinstance(exclude, list) or any(not isinstance(t, str) for t in exclude):
        return None
    include = _parse_policy_include(answer.get("include"))
    if include is None:
        return None
    return ToolPolicy(frozenset(exclude), "", included=include)


def _left_to_the_server(
    command: str,
    session_key: str,
    agent_name: str,
    *,
    code: str,
    place: str,
    private_member: bool,
) -> str:
    """Why this gateway builds no list for the caller, or ``""`` when it may.

    Each reason is a case where building the list here is not shown to be safe, or the
    list is not shown to be the server's: it is answered by name, and the server builds
    its own. The install comes first: nothing below it, and no listing module, runs for
    a tree a session's agent could have written.
    """
    if not _package_tree_is_sealed():
        return "an install whose package directory the sandbox does not seal"
    if command not in EARLY_ANSWERS:
        return "unknown server"
    if not _CODE_AT_START:
        return "this gateway has not read its own code yet"
    if not code or code != _CODE_AT_START:
        return "the server runs other code than this gateway"
    if not place or place != mcp_listing_relay.place_stamp():
        return "the server reads another data home or agents directory"
    if private_member:
        return "a private member session"
    context = current_context()
    if context.profile != PROFILE_STANDALONE:
        return "a platform overlay"
    if getattr(context, "governance", None) is not None:
        return "a governance policy"
    if resolve_active_scope(session_key, agent=agent_name) is not None:
        return "a governance profile bound to this session"
    return ""


def session_listing_body(
    policy: Mapping[str, Any] | None,
    command: str,
    session_key: str,
    agent_name: str,
    *,
    code: str,
    place: str,
    private_member: bool,
    unregistered: bool = False,
) -> str:
    """The JSON body of a policy answer that carries *command*'s listing. Blocking.

    *policy* is the answer the route gives without the parameter (``None`` for an agent
    with no policy); it is repeated unchanged, with ``listing`` beside it. Runs in a
    worker thread, serialisation included: a ``kirocrew-core`` list is over a hundred
    kilobytes and its descriptions read the configuration and the agents directory.

    *unregistered* is a session whose start has not registered it yet. Its own policy
    read is answered "agent not resolved" in that window, so a server that builds its
    list then lists everything. The body carries no policy for such a session, and a
    list only where the policy withholds none of this server's tools, which is that
    same unfiltered list.
    """
    body: dict[str, Any] = {} if unregistered else dict(policy or {})
    try:
        body[mcp_listing_relay.LISTING_KEY] = _listing(
            policy, command, session_key, agent_name, code, place, private_member, unregistered
        )
    except Exception:  # noqa: BLE001 - the policy answer stands; the server builds its list
        logger.warning("could not build %s's tool list for its session", command, exc_info=True)
        body[mcp_listing_relay.LISTING_KEY] = {
            mcp_listing_relay.LOCAL_KEY: "the list could not be built here"
        }
    return json.dumps(body)


def _listing(
    policy: Mapping[str, Any] | None,
    command: str,
    session_key: str,
    agent_name: str,
    code: str,
    place: str,
    private_member: bool,
    unregistered: bool,
) -> dict[str, Any]:
    reason = _left_to_the_server(
        command, session_key, agent_name, code=code, place=place, private_member=private_member
    )
    parsed = _policy_of(policy) if not reason else None
    if parsed is None:
        return {mcp_listing_relay.LOCAL_KEY: reason or "a policy the server must read itself"}
    module, attribute = EARLY_ANSWERS[command]
    server: StdioServer = getattr(importlib.import_module(module), attribute)
    # A worker thread runs in a copy of the request's context, so the caller set here
    # is this request's alone. Restored all the same, on failure too: no identity
    # outlives the build it was set for.
    previous = current_caller()
    set_current_caller(
        CallerContext(
            session_key=session_key,
            session_type=session_key.partition(":")[0],
            from_gateway=True,
        )
    )
    try:
        with mcp_listing_relay.as_a_tool_server():
            listed = server.list_tools()
            tools = within_policy(server.name, listed, parsed)
    finally:
        set_current_caller(previous)
    if unregistered and len(tools) != len(listed):
        return {
            mcp_listing_relay.LOCAL_KEY: "the session is not registered yet and its "
            "agent's policy withholds tools of this server"
        }
    return {
        "initialize": initialize_result(
            server.name,
            server.version,
            advertise_caller_identity=server.advertise_caller_identity,
            reports_inflight=server.reports_inflight,
            instructions=server.instructions,
        ),
        "tools": tools,
    }
