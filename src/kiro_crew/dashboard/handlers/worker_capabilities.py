"""``worker_capabilities``: what a worker this caller dispatches could reach, read first.

``GET /api/session-control/worker-capabilities[?agent=&query=]`` answers, for the
calling session, the MCP server names of the worker its next ``session_create`` /
``work_dispatch`` would open (:func:`kiro_crew.mate_capability_catalog.lookup`),
or, with a ``query``, whether that worker has a matching server or tool. So a
conductor checks before it hands work off, or before it tells the user something
is not available, instead of learning it from the dispatch result.

Which worker is the one ``session_create`` would open for the same ``agent``:

* a Bot's own chat, or its worker holding the conducting kit, hands off to the
  Bot's worker whatever agent it names (``session_create._hands_off_to_own_worker``),
  so the answer is that worker's projected spec (``mate_worker.member_worker_spec``);
* any other caller gets the spec the session MCP translation reads for *agent*
  (the caller's own agent when none is named, resolved as ``session_create``
  resolves it) in the caller's project, as a session on the worker's backend
  reads it (``agent_sdk.worker_session_spec``).

A read: it changes nothing and leads to no card. The caller passes the gates a
targetless session-control verb passes (``session_status``'s): the internal secret
(``_require_internal``), then ``refuse_caller_identity`` and ``refuse_caller_surface``
(session control on or a dispatch bypass, no unattended, incognito, temporary,
app-scoped or channel-linked caller), so a channel-mirrored chat cannot post the
user's server names into a shared thread. The answer is about its own next
worker only.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.dashboard.handlers.session_control import _counted, _refusal, _require_internal

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

#: Longest ``agent`` taken. A name, never a path: the spec lookup refuses a
#: path-shaped one too (``agent_parts.spec_lookup``), this only answers it plainly.
_AGENT_MAX = 128
_NOT_A_NAME = re.compile(r"[/\\\x00-\x1f\x7f]")
_BACKEND_MAX = 64


def _bad_request(code: str, error: str) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=400)


def _own_worker_member(slot: Any, agent: str, kit: bool) -> str:
    """The Bot whose worker this caller's dispatch of *agent* opens, else ``""``.

    *kit* is :func:`mate_worker_conducts.holds_conducting_kit` for the caller, as
    ``session_create`` reads it. Blocking.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.crewmate_approval import crewmate_name_for
    from kiro_crew.dashboard.session_control_parts.session_create import (
        _hands_off_to_own_worker,
    )
    from kiro_crew.execution_context import read_session_execution

    execution = read_session_execution(slot_history_key(slot))
    if not _hands_off_to_own_worker(execution, agent, "", kit):
        return ""
    return crewmate_name_for(KiroCrewConfig.load(), execution)


def _backend(backend: str) -> str:
    """The runtime id of the backend the worker spawns on: *backend* as a caller
    names it (``kiro`` is the stored spelling of the runtime ``""``), translated as
    ``agent.acp_backend`` is (``agent_sdk.backends.resolve_selected_backend``), else
    the configured ``agent.acp_backend``, already in runtime form.

    shortcut: a per-agent or per-crew backend pin is not read, only the caller's
    explicit backend or the global one; read the pin if a lookup is reported wrong.
    Blocking (a config read).
    """
    from kiro_crew.agent_sdk.backends import backend_from_stored, resolve_selected_backend
    from kiro_crew.config.loader import KiroCrewConfig

    if backend:
        return resolve_selected_backend(backend_from_stored(backend))
    return str(KiroCrewConfig.load().agent.acp_backend or "")


def _spec_agent(name: str, project: str | None) -> str:
    """The kiro agent a session of *name* runs, resolved as ``session_create`` resolves it.

    A crew member's name is its roster key, not a spec file, so it maps to its bound
    agent (``config.loader_agents.resolve_agent_bindings``); the stock worker is a
    template pick, as ``session_create`` reads a bare pick of it. ``""`` when the name
    resolves to no agent. Blocking (a config read).
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.loader_agents import resolve_agent_bindings
    from kiro_crew.dashboard.session_control_parts.session_create import _STOCK_WORKER_AGENT

    # An empty name is the default agent, as ``session_create`` resolves it.
    bindings = resolve_agent_bindings(
        KiroCrewConfig.load(),
        name,
        project,
        validate_memory_files=False,
        selection_kind="template" if name == _STOCK_WORKER_AGENT else "",
    )
    return str(bindings.kiro_agent or "") if bindings.requested_resolved else ""


def _answer(
    slot: Any, agent: str, query: str, kit: bool, backend: str = ""
) -> dict[str, Any] | None:
    """The lookup for this caller's next worker of *agent*, or ``None`` when none resolves."""
    from kiro_crew import mate_capability_catalog as catalog_mod
    from kiro_crew.mate_worker import MATE_WORKER_AGENT, member_worker_spec

    member = _own_worker_member(slot, agent, kit)
    if member:
        spec: dict[str, Any] | None = member_worker_spec(member)
        name = MATE_WORKER_AGENT
    else:
        from kiro_crew.agent_sdk.worker_session_spec import worker_session_spec

        name = agent or str(getattr(slot, "agent", "") or "")
        project = str(getattr(slot, "project", "") or "") or None
        name = _spec_agent(name, project)
        spec = worker_session_spec(name, _backend(backend), project) if name else None
    if spec is None:
        return None
    return {"agent": name, **catalog_mod.lookup(spec, query)}


@_counted("other")
async def api_session_control_worker_capabilities(request: web.Request) -> web.Response:
    """GET /api/session-control/worker-capabilities — the caller's next worker's servers."""
    from kiro_crew import mate_capability_catalog as catalog_mod
    from kiro_crew.agent import DerivedSpecStale
    from kiro_crew.dashboard import session_control as sc
    from kiro_crew.dashboard.handlers._shared import _read_session_key
    from kiro_crew.mate_worker import WorkerKitUnreadable
    from kiro_crew.mate_worker_conducts import holds_conducting_kit

    refused = await _require_internal(request)
    if refused is not None:
        return refused
    agent = request.query.get("agent", "").strip()
    query = request.query.get("query", "")
    backend = request.query.get("backend", "").strip()
    if len(agent) > _AGENT_MAX or _NOT_A_NAME.search(agent):
        return _bad_request("invalid_agent", "'agent' is an agent name")
    if len(backend) > _BACKEND_MAX or _NOT_A_NAME.search(backend):
        return _bad_request("invalid_backend", "'backend' is a backend name")
    if len(query) > catalog_mod.LOOKUP_QUERY_MAX:
        return _bad_request(
            "invalid_query", f"'query' is text of at most {catalog_mod.LOOKUP_QUERY_MAX} characters"
        )
    state: DashboardState = request.app["state"]
    caller = _read_session_key(request)
    # The last await before the synchronous caller gates, as ``session_status``'s route.
    await sc.prewarm_enabled_check()
    try:
        deny = sc._deny_factory(
            caller_session_key=caller, operation="worker_capabilities", target=""
        )
        caller_key = sc.refuse_caller_identity(state, caller_session_key=caller, deny=deny)
        slot = sc.refuse_caller_surface(state, caller_key=caller_key, deny=deny)
    except sc.SessionControlError as exc:
        return _refusal(exc)
    kit = holds_conducting_kit(state, slot)
    try:
        answer = await asyncio.to_thread(_answer, slot, agent, query, kit, backend)
    except DerivedSpecStale:
        # The session's own read refuses a derived spec that predates the default
        # (``kiro_crew.agent.require_fresh_derived_spec``); a spawn
        # would refuse it too, so the lookup says so rather than guess.
        return web.json_response(
            {
                "error": "that worker's agent spec is out of date, so its tools are unknown "
                "until the gateway rewrites it",
                "code": "worker_spec_stale",
            },
            status=409,
        )
    except (ValueError, OSError, WorkerKitUnreadable):
        # An unreadable execution record, config or Bot worker kit, named rather
        # than a 500 (``session_create`` names the first two the same way).
        return web.json_response(
            {"error": "caller execution context is unavailable", "code": "memory_unavailable"},
            status=409,
        )
    if answer is None:
        return web.json_response(
            {"error": f"{agent or 'your agent'!r} has no agent spec", "code": "agent_unresolved"},
            status=404,
        )
    return web.json_response(answer)
