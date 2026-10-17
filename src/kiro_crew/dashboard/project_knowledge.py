"""Resolve a calling session to its project knowledge namespace, server-side.

Project-scoped knowledge (see ``knowledge/project_scope.py``) files a session's
knowledge under a namespace derived from the ROOT folder its chat is filed in.
The MCP tools that search and add knowledge run in the stateless ``mcp-core``
server, which holds neither the live slot table nor the folder tree, so they ask
the gateway: "what project namespace does my own session belong to?" This module
is the gateway's answer.

The one rule that matters for safety: the caller's identity is the
``X-Session-Key`` the gateway already verified (kernel-attested on the unix
socket, bearer-token on TCP), NEVER a folder or namespace the model could put in
a tool argument. A session can only ever resolve to ITS OWN project. That is why
the MCP tools expose no project/folder parameter and the route re-asserts
``internal_auth`` before trusting the header.

Resolution walks from a session key to a folder id through the same spellings
the rest of the dashboard already handles, then roots that folder and derives
the namespace:

1. A sub-agent (``subagent:<id>`` or a continuable run key) has no folder of its
   own -- it inherits its PARENT's, the same destination its completion events
   and file cards already route to. We resolve the parent key and continue with
   it, so a worker spawned under a project session searches that project.
2. A live dashboard/channel/cron tab carries ``folder_id`` on its slot.
3. A session with no open tab (archived, or a headless cron) carries the same
   ``folder_id`` on its on-disk record, read back by key.

A session filed in no folder, or one whose folder roots to nothing, resolves to
``""`` -- no project namespace -- and the caller then searches exactly as it does
without a project.

The live-slot and roster reads are in-memory and run on the event loop; the
durable on-disk read (step 3) stats and reads transcript metadata, so it is
offloaded with ``asyncio.to_thread``. ``resolve_session_project_namespace`` is
the async entry point every handler uses; the sync ``_resolve_live`` half is
exposed for tests that drive a fake state without an event loop.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.chat_utils_slots import dashboard_slot_key
from kiro_crew.dashboard.folder_ownership import project_root_folder_id
from kiro_crew.dashboard.handlers.files import (
    _SUBAGENT_SESSION_PREFIX,
    _subagent_parent_session_key,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.knowledge.project_scope import project_namespace

logger = logging.getLogger(__name__)

#: A sub-agent is chased to its parent at most this many times before we give up
#: and resolve to the global scope. A correct roster is at most one hop
#: (sub-agents do not nest here), so anything beyond a small bound is a corrupt
#: or cyclic roster, which must degrade to "no project" rather than loop.
_MAX_SUBAGENT_HOPS = 8


def _resolve_to_session_key(state: DashboardState, session_key: str) -> str:
    """Chase a sub-agent key to the parent session whose folder it inherits.

    Returns the non-sub-agent session key to resolve a folder for, or ``""`` when
    the chain dead-ends (unknown run, self-reference, or a pathological roster).
    In-memory roster reads only, so it is safe on the event loop.
    """
    hops = 0
    seen: set[str] = set()
    while session_key.startswith(_SUBAGENT_SESSION_PREFIX) and hops < _MAX_SUBAGENT_HOPS:
        if session_key in seen:
            return ""
        seen.add(session_key)
        parent = _subagent_parent_session_key(state, session_key)
        if not parent or parent == session_key:
            return ""
        session_key = parent
        hops += 1
    if session_key.startswith(_SUBAGENT_SESSION_PREFIX):
        # Still a sub-agent key after the hop bound: a pathological roster.
        return ""
    return session_key


def _live_slot_folder_id(state: DashboardState, session_key: str) -> str:
    """The folder id of *session_key*'s LIVE slot, or ``""`` when it has no tab.

    In-memory slot-table read; safe on the event loop. A session with no live
    slot (archived, revived, or a headless cron) returns ``""`` here and is
    resolved from the durable record by :func:`_durable_folder_id`.
    """
    slot_key = dashboard_slot_key(session_key)
    if not slot_key:
        return ""
    slot = state.get_slot(slot_key)
    if slot is None:
        return ""
    folder_id = getattr(slot, "folder_id", "") or ""
    return folder_id if isinstance(folder_id, str) else ""


def _durable_folder_id(state: DashboardState, session_key: str) -> str:
    """The folder id on *session_key*'s ON-DISK record, or ``""``.

    Stats and reads transcript metadata through ``conversation_log``, so the
    caller MUST invoke it off the event loop (``asyncio.to_thread``). The lookup
    is keyed, so ``list_sessions(keys=[session_key])`` returns at most the one row
    asked for -- its ``key`` field is the file STEM (``dashboard_abc`` for
    ``dashboard:abc``), not the original key, so the row is taken positionally
    rather than matched on ``key``.
    """
    log = getattr(state, "conversation_log", None)
    if log is None:
        return ""
    try:
        rows = log.list_sessions(keys=[session_key])
    except Exception:  # noqa: BLE001 -- a resolver must never fail the search
        logger.debug("project-knowledge: session lookup failed", exc_info=True)
        return ""
    if not rows:
        return ""
    folder_id = str(rows[0].get("folder_id") or "")
    return folder_id


def _namespace_for_folder(state: DashboardState, folder_id: str) -> str:
    """Root *folder_id* over the live folder tree and derive its project namespace.

    In-memory dict-list read; safe on the event loop. A nested (sub-team /
    worker) folder resolves to its top-level project so it inherits that
    project's knowledge.
    """
    if not folder_id:
        return ""
    folders = getattr(state, "_folders", None)
    if not isinstance(folders, list):
        folders = []
    return project_namespace(project_root_folder_id(folders, folder_id))


def _resolve_live(state: DashboardState, session_key: str) -> tuple[str, str]:
    """The loop-safe half of resolution: ``(namespace, pending_durable_key)``.

    Resolves everything that reads in-memory state (sub-agent chase, live slot,
    folder root). When the session has a LIVE slot the namespace is returned and
    the second value is ``""``. When it has no live slot the namespace is ``""``
    and the second value is the session key whose DURABLE record must be read off
    the loop -- or ``""`` when there is nothing left to resolve (empty key, a
    dead-ended sub-agent chase).
    """
    session_key = (session_key or "").strip()
    if not session_key:
        return "", ""
    resolved = _resolve_to_session_key(state, session_key)
    if not resolved:
        return "", ""
    folder_id = _live_slot_folder_id(state, resolved)
    if folder_id:
        return _namespace_for_folder(state, folder_id), ""
    return "", resolved


async def resolve_session_project_namespace(state: DashboardState, session_key: str) -> str:
    """The project knowledge namespace for *session_key*, or ``""`` for none.

    *session_key* MUST be the gateway-verified caller identity, not a value from
    a tool argument. Async because a session with no open tab needs its durable
    record read, which stats and reads transcript metadata and must not run on
    the event loop; the live-slot, roster and folder-tree reads stay on the loop.
    Any dead end -- an unknown session, a session filed nowhere, a missing
    manager -- yields ``""``, so the caller falls back to the global (unscoped)
    search that is identical to the behaviour without a project.
    """
    namespace, pending = _resolve_live(state, session_key)
    if namespace or not pending:
        return namespace
    folder_id = await asyncio.to_thread(_durable_folder_id, state, pending)
    return _namespace_for_folder(state, folder_id)


async def session_project_namespace_route(request: web.Request) -> web.Response:
    """GET /api/knowledge/session-project-namespace -- the caller's project namespace.

    An internal (MCP) read: the stateless knowledge tools call it to scope their
    search/add to the caller's project. The identity is the gateway-verified
    ``X-Session-Key`` header, resolved here; the response carries only the
    derived namespace (``""`` when the session is filed in no project), never the
    folder id or any other session's data.

    The route re-asserts ``internal_auth`` before trusting the header. It is a
    strict-internal path, but a ``local_only=False`` deployment reclassifies
    strict paths as mixed, so a cookie-authed browser could otherwise reach the
    handler and name any session as itself. Only a caller the middleware verified
    on the internal-secret handshake carries ``internal_auth is True``; every
    other caller is refused here, the same fence sibling internal routes apply.
    """
    if request.get("internal_auth") is not True:
        return web.json_response(
            {"error": "internal_auth required", "code": "internal_auth_required"},
            status=403,
        )
    state: DashboardState = request.app["state"]
    session_key = request.headers.get("X-Session-Key", "").strip()
    namespace = await resolve_session_project_namespace(state, session_key)
    return web.json_response({"namespace": namespace})
