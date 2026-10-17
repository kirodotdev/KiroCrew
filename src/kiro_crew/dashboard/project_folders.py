"""Sidebar Projects: folders whose chats are locked to the conductor agent.

A Project is stored as an ordinary chat folder carrying ``kind: "project"``.
The flag is set once, when the sidebar's New > Project creates the folder, and
no route can add or remove it afterwards. A Project sits at the top level and
holds chats only (:func:`project_nesting_error`); the ancestor walk that decides
whether a folder is *in* one still follows ``parent_id``, so a folder row from a
damaged store that sits under a Project is locked the same way.

Inside a project:

* every NEW chat is bound to :data:`PROJECT_AGENT` server-side, whatever agent
  the request named -- the dashboard create route, ``session_create`` and a
  never-run chat moved into the project all land on it;
* the one exception is a conductor's own workers: a session that a
  :data:`PROJECT_AGENT` chat in the same project creates keeps the agent it
  asked for, and is filed in its creator's folder so the sidebar draws it
  nested under that conductor instead of in an agent-named subfolder;
* no chat's agent can be switched, except to :data:`PROJECT_AGENT`.

Everything here is a dict walk over the folder rows, so it is safe on the
event loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from kiro_crew.sel import sel

if TYPE_CHECKING:  # aiohttp is imported where a response is built: the MCP
    # server reads the folder predicates here and has no web stack of its own.
    from aiohttp import web

#: The ``kind`` value that marks a folder as a Project.
PROJECT_KIND = "project"

#: The one agent a new chat in a project may run on.
PROJECT_AGENT = "kirocrew-conductor"

#: The selection namespace :data:`PROJECT_AGENT` is bound in. A crewmate may
#: carry the same name (its page template is shipped under it), and a bare name
#: resolves to the member first, so the lock always names the shared template.
PROJECT_AGENT_KIND = "template"

#: Refusal code for an agent change inside a project.
PROJECT_AGENT_LOCKED = "project_agent_locked"


def validate_folder_kind(raw: Any) -> tuple[str, str | None]:
    """``(kind, error)`` for a folder-create ``kind`` field.

    Absent, ``None`` and ``""`` mean an ordinary folder; ``"project"`` makes a
    Project. Anything else is refused rather than stored, so the field keeps
    exactly two on-disk shapes: absent, or ``"project"``.
    """
    if raw is None or raw == "":
        return "", None
    if raw == PROJECT_KIND:
        return PROJECT_KIND, None
    return "", 'kind must be "project" or omitted'


def is_project_folder(folder: Any) -> bool:
    """Whether one folder row is itself a Project."""
    return isinstance(folder, dict) and folder.get("kind") == PROJECT_KIND


def is_project_agent(agent: Any, agent_kind: Any) -> bool:
    """Whether *agent* in namespace *agent_kind* is :data:`PROJECT_AGENT`, the template.

    A crewmate that happens to be named ``kirocrew-conductor`` is not: it runs on
    its own private memory, not the shared template every project chat runs on.
    """
    return agent == PROJECT_AGENT and agent_kind != "member"


def conductor_home_folder(caller_key: str, slot_rows: Iterable[Any], folders: Iterable[Any]) -> str:
    """The folder a project conductor files its workers in, from API rows, or ``""``.

    The MCP ``session_create`` reads it before walking a ``<goal>/<agent>``
    path: a conductor in a project has its workers filed beside it by the
    create route, so creating those path segments would only leave empty
    agent-named folders in the project. *slot_rows* are ``/api/chat/slots``
    rows and *caller_key* the caller's ``dashboard:<slot>`` key.
    """
    slot_key = caller_key.split(":", 1)[-1]
    folder_rows = list(folders)
    for row in slot_rows:
        if not isinstance(row, dict) or str(row.get("key") or "") != slot_key:
            continue
        folder_id = str(row.get("folder_id") or "")
        if is_project_agent(row.get("agent"), row.get("agent_kind")) and project_root_id(
            folder_rows, folder_id
        ):
            return folder_id
        return ""
    return ""


def project_root_id(folders: Iterable[Any], folder_id: str) -> str:
    """The id of the Project *folder_id* sits in (itself or nearest ancestor), or ``""``.

    Cycle-guarded like the other ancestor walks: a damaged ``parent_id`` loop
    ends the walk instead of spinning.
    """
    if not folder_id:
        return ""
    by_id = {str(f.get("id") or ""): f for f in folders if isinstance(f, dict) and f.get("id")}
    seen: set[str] = set()
    current = folder_id
    while current and current not in seen:
        seen.add(current)
        folder = by_id.get(current)
        if folder is None:
            return ""
        if is_project_folder(folder):
            return current
        current = str(folder.get("parent_id") or "")
    return ""


def project_admits_agent(
    folders: Iterable[Any], folder_id: str, agent: Any, agent_kind: Any = ""
) -> bool:
    """Whether a chat born on *agent* (in namespace *agent_kind*) may be filed in *folder_id*.

    For the creation paths that inherit or configure an agent rather than take
    one from a person -- a fork copies its parent's, a cron tab and a channel
    arrival carry their configured one. Outside a project every agent is
    admitted; inside one only :data:`PROJECT_AGENT` is, and such a chat lands
    unfiled instead of starting a project chat on another agent.
    """
    return is_project_agent(agent, agent_kind) or not project_root_id(folders, folder_id)


def slot_project_id(state: Any, slot: Any) -> str:
    """The Project a chat slot is filed in, or ``""``."""
    folder_id = getattr(slot, "folder_id", "") if slot is not None else ""
    if not isinstance(folder_id, str) or not folder_id:
        return ""
    return project_root_id(getattr(state, "_folders", None) or [], folder_id)


def is_project_conductor(state: Any, slot: Any, project_id: str) -> bool:
    """Whether *slot* is a :data:`PROJECT_AGENT` chat filed in *project_id*."""
    return (
        bool(project_id)
        and is_project_agent(getattr(slot, "agent", ""), getattr(slot, "agent_kind", ""))
        and slot_project_id(state, slot) == project_id
    )


def project_session_target(
    state: Any, caller_slot: Any, folder_id: str, agent: str, agent_kind: str
) -> tuple[str, str, str]:
    """``(folder_id, agent, agent_kind)`` for a session another session creates.

    * The caller is a project conductor: its worker is filed in the caller's own
      folder, whatever folder it named (or none), so the sidebar draws it nested
      under its creator rather than in a ``<goal>/<agent>`` subfolder, and it
      keeps the agent it asked for.
    * Otherwise, a create into a project is bound to :data:`PROJECT_AGENT`.
    * Anything else passes through unchanged.
    """
    caller_project = slot_project_id(state, caller_slot)
    if is_project_conductor(state, caller_slot, caller_project):
        return caller_slot.folder_id, agent, agent_kind
    if project_root_id(getattr(state, "_folders", None) or [], folder_id):
        return folder_id, PROJECT_AGENT, PROJECT_AGENT_KIND
    return folder_id, agent, agent_kind


def project_pins_agent(
    state: Any, slot: Any, agent_name: Any, agent_kind: Any = PROJECT_AGENT_KIND
) -> bool:
    """Whether moving *slot* to *agent_name* would break its project's agent lock.

    Allowed inside a project: the project agent, except as a crewmate (a
    same-named member would run on its private memory, not the shared template),
    and a same-name reset of the agent a chat that already ran keeps. The predicate
    :func:`project_agent_switch_refusal` answers with a 409, for the one writer no
    HTTP guard reaches: kiro-cli reporting it already switched, which names a
    template, the default kind.
    """
    if not slot_project_id(state, slot):
        return False
    if agent_name == PROJECT_AGENT:
        return agent_kind == "member"
    if agent_name != getattr(slot, "agent", None):
        return True
    # A same-name reset stays in the namespace the chat is bound in: the same
    # name in another one is a re-bind by another spelling.
    return bool(agent_kind) and agent_kind != (getattr(slot, "agent_kind", "") or agent_kind)


def project_switch_kind(state: Any, slot: Any, agent_name: Any, agent_kind: Any) -> Any:
    """The namespace a switch of a project chat to *agent_name* binds in.

    A bare name in a project never resolves somewhere new: ``kirocrew-conductor``
    binds as the template, as every other path into a project binds it, and a
    same-name reset keeps the chat's own namespace. A bare name resolves to a
    same-named crewmate first, which would move the chat onto its memory.
    """
    if agent_kind or not slot_project_id(state, slot):
        return agent_kind
    if agent_name == PROJECT_AGENT:
        return PROJECT_AGENT_KIND
    if agent_name == getattr(slot, "agent", None):
        return getattr(slot, "agent_kind", "") or agent_kind
    return agent_kind


def project_agent_switch_refusal(
    state: Any, slot: Any, agent_name: Any, agent_kind: Any, *, caller: str = "dashboard"
) -> web.Response | None:
    """409 when *slot* is in a project and the switch would leave the project agent.

    Switching TO the project agent stays allowed (it is the locked value), and
    so does a same-name reset, which re-binds nothing.
    """
    if not project_pins_agent(state, slot, agent_name, agent_kind):
        return None
    from aiohttp import web

    sel().log_api_access(
        caller=caller,
        operation="chat.slot_agent",
        outcome="denied",
        source="project_lock",
        resources=f"slot={getattr(slot, 'key', '')}",
        error="chats in a project run on the project agent",
    )
    return web.json_response(
        {
            "error": f"chats in a project run on {PROJECT_AGENT}; the agent cannot be changed",
            "code": PROJECT_AGENT_LOCKED,
        },
        status=409,
    )


#: Refusal code for a folder nesting a Project, or a folder nested in one.
PROJECT_TOP_LEVEL = "project_top_level"

#: Why a placement was refused, by the sentinel :func:`project_nesting_error` returns.
PROJECT_NESTING_ERRORS = {
    "project_nested": "a project sits at the top level of the sidebar, not inside a folder",
    "project_parent": "a project holds chats, not folders",
}


def project_nesting_error(folders: Iterable[Any], is_project: bool, parent_id: str) -> str:
    """Why a folder may not sit under *parent_id*, as a :data:`PROJECT_NESTING_ERRORS` key, or ``""``.

    Projects are the sidebar's own top-level section: a Project never sits inside
    a folder, and nothing is filed in a Project but chats -- its conductor's
    workers nest under it as a session tree, not as folders. So both a folder
    created and a folder moved are refused under a Project, and a Project is
    refused under anything. The top level (``""``) is always allowed.
    """
    if not parent_id:
        return ""
    if is_project:
        return "project_nested"
    return "project_parent" if project_root_id(folders, parent_id) else ""


def project_nesting_refusal(code: str) -> web.Response:
    """The 409 for a :func:`project_nesting_error` sentinel."""
    from aiohttp import web

    return web.json_response(
        {"error": PROJECT_NESTING_ERRORS[code], "code": PROJECT_TOP_LEVEL}, status=409
    )


async def bind_unstarted_slot_for_project(
    state: Any, request: web.Request, slot: Any, name: str, folder_id: str
) -> web.Response | None:
    """Put a never-run chat that is being filed INTO a project on the project agent.

    Moving a brand-new chat into a project and then sending its first message
    would otherwise start a project chat on any agent, so the move switches it
    first, through the same transaction the agent picker uses. A chat that has
    already run is an existing conversation and keeps its agent; so does a chat
    moving within the project it is already in (an idle worker included).
    Returns the switch's refusal, or ``None`` to let the move proceed.
    """
    target = project_root_id(getattr(state, "_folders", None) or [], folder_id)
    if not target or slot_project_id(state, slot) == target:
        return None
    if is_project_agent(getattr(slot, "agent", ""), getattr(slot, "agent_kind", "")):
        return None
    if getattr(slot, "total_messages", 0) or getattr(slot, "running", False):
        return None
    # Imported here: the chat-handler facade imports this module. Read through the
    # facade, the one import path its owners allow.
    from kiro_crew.dashboard import chat_handlers

    resp = await chat_handlers.switch_slot_agent(
        state,
        slot,
        name,
        PROJECT_AGENT,
        PROJECT_AGENT_KIND,
        chat_handlers.SlotAgentSwitchCaller.for_request(request),
    )
    ok, _ = chat_handlers.agent_switch_outcome(resp)
    return None if ok else resp
