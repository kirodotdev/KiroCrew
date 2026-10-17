"""A chat folder's ``default_crew``: which registered crew its new chats start on.

The field holds the instance id of one registered crew (the Remote crew
sessions preview), and ``""`` means Local, the same as a folder that never
carried the key. It is stored and returned verbatim: the dashboard reads it to
route the folder's New chat to *New chat on crew*, and nothing server-side
creates a session from it, so a crew that was removed since leaves a value that
simply names no crew. It is normalised by ``instances.crew_ref.clean_crew_ref``,
like the sibling ``default_destination_crew``.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard.chat_folders_paths import refuse_person_only_field
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request


def refuse_principal_default_crew(
    request: web.Request,
    request_app: str,
    default_crew: str,
    *,
    operation: str,
    folder_id: str,
) -> web.Response | None:
    """Only the dashboard OWNER may pin a folder to a crew; refuse everyone else.

    The field decides which machine runs the folder's future chats, with that
    machine's agents, files and credentials, and a turn that already ran there
    cannot be taken back. Folder permission is not that permission: an app or an
    admitted crew member that may edit its own folders must not be able to send
    the owner's next New chat in it to another machine. Nor may an allow-listed
    NON-owner person: the peer control plane refuses them a crew session
    directly (``handlers_instances_gate``), and a pin they wrote would have the
    owner's own New chat create one for them. So a NON-EMPTY ``default_crew``
    (already cleaned) is accepted only from a request ``is_owner_dashboard_request``
    admits that carries no app/member principal and not the internal credential
    every MCP tool call carries; everything else is refused at both write sites
    and audited as denied. Clearing to ``""`` stays allowed: it only returns the
    folder to Local, and the folder's ownership checks still decide who may.
    """
    if not default_crew:
        return None
    agent = request.get("internal_auth") is True
    if not (request_app or agent) and is_owner_dashboard_request(request):
        return None
    caller = request_app or ("internal" if agent else "dashboard-non-owner")
    return refuse_person_only_field(
        caller,
        operation=operation,
        resources=f"folder={folder_id or '-'} default_crew=set",
        audit_error="default_crew may be set only by the dashboard owner",
        message=(
            "default_crew may be set only from the dashboard owner's own session: "
            "folder permission does not choose the machine its chats run on"
        ),
        code="default_crew_forbidden",
    )
