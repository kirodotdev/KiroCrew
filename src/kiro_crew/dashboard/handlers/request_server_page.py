"""``request_server`` for a server on the MCP page: the agent asks to add, change or remove it.

An agent cannot write the MCP registries (``security.paths_lists._MCP_REGISTRY_ROWS``
and the read-only seal on ``<data home>/mcp.json``), so this is how it asks. The
agent half (``POST /api/cards/agent/request-server`` with ``action`` ``change`` or
``remove``, or ``source`` ``custom``) is routed here by
:func:`handlers.template_server_request.api_cards_agent_request_server`, which has
already resolved the verified caller. It raises an ``mcp.server`` card
(:mod:`kiro_crew.change_card_mcp_server`) in the caller's own chat through the
same :func:`handlers.change_cards.propose_for_caller` every card uses. Nothing is
written until the person presses Apply; the card's steps are the MCP page's own
owner-only routes.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from aiohttp import web

from kiro_crew.change_card_mcp_server import (
    ACTION_ADD,
    ACTIONS,
    KIND_MCP_SERVER,
    OPTION_USE_NOW,
    PARAM_PROJECT_FILE,
    PARAM_PROJECT_FOLDER,
    PARAM_TARGET,
    PARAM_USES_CHAT_FOLDER,
    TARGET_PROJECT,
)
from kiro_crew.dashboard.handlers.guide import _audit, _deny

#: The request names this flow, as opposed to a template addition.
SOURCE_CUSTOM = "custom"


def is_page_request(body: dict[str, Any]) -> bool:
    """Whether a ``request_server`` body asks about a server on the MCP page."""
    action = body.get("action", ACTION_ADD)
    return action != ACTION_ADD or body.get("source") == SOURCE_CUSTOM


def server_state(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """The card's snapshot of server *name*. Blocking.

    A ``project`` add (*params* ``target``) reads only the chat folder's MCP file:
    ``project`` is its ``exists`` and ``corrupt``, and ``has_server`` for *name*.

    ``exists``: the name is on the MCP page in any store. ``editable``: it is in the
    Kiro Crew store, the one a change writes. ``entry``: that stored entry without
    its enabled flag or authorship marker. ``carried``: the fields of it the page's
    save keeps as stored (not editable there, or header values already set).
    ``disabled``: the stored entry is muted by the launch predicate.
    ``scopes``: fingerprints of the source entries an uninstall removes.
    ``sharing_on``: MCP server sharing is on for this Kiro Crew; ``shared``: the
    server is shared now; ``cwd_use``: its recorded project-files answer.
    ``share_exclusive``: no other source declares the name (or its broker stem).
    """
    if (params or {}).get(PARAM_TARGET) == TARGET_PROJECT:
        from kiro_crew.change_card_mcp_server import project_file_path
        from kiro_crew.dashboard.handlers.mcp_project_file import read_project_servers

        read = read_project_servers(project_file_path(params or {}))
        return {
            "project": {
                "exists": read["exists"],
                "corrupt": read["corrupt"],
                "has_server": name in read["servers"],
            }
        }
    from kiro_crew.dashboard.change_cards_readers import _mcp_exists, _mcp_scopes
    from kiro_crew.dashboard.handlers.mcp import _get_kirocrew_entry
    from kiro_crew.dashboard.handlers.mcp_custom import _update_carried
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted
    from kiro_crew.mcp_provenance import MARKER_KEY

    sharing = {**_sharing_state(name), "share_exclusive": not _has_other_declaration(name)}
    stored = _get_kirocrew_entry(name)
    scopes = _mcp_scopes(name)
    if stored is None:
        return {"exists": _mcp_exists(name), "editable": False, "scopes": scopes, **sharing}
    entry = {k: v for k, v in stored.items() if k not in ("disabled", MARKER_KEY)}
    carried = sorted(_update_carried(stored))
    return {
        "exists": True,
        "editable": True,
        "entry": entry,
        "scopes": scopes,
        "carried": carried,
        "disabled": mcp_entry_is_muted(stored),
        **sharing,
    }


def _has_other_declaration(name: str) -> bool:
    """A card writes only the Crew store, so it cannot approve another source.

    The specs Kiro Crew writes itself (``agent_files.OWNED_KIRO_AGENT_FILES``, the
    crewmate service specs) and its derived copies (skill views, app launch copies,
    the side chat's read-only spec) are skipped: they copy the store's servers, so
    a store server would always look declared twice. Any other rendered copy counts
    conservatively as a declaration. An unreadable source keeps the server
    per-chat; the stub route independently checks the resolved launch against the
    card's entry at Apply.
    """
    from kiro_crew.agent import _app_owned_mcp_keys
    from kiro_crew.agent_files import MATE_SERVICE_AGENT_FILENAMES, OWNED_KIRO_AGENT_FILES
    from kiro_crew.agent_spec_format import (
        is_app_agent_launch_name,
        is_native_skill_alias_name,
        iter_agent_spec_files_strict,
    )
    from kiro_crew.config.paths import kiro_agents_dir, mate_agents_dir
    from kiro_crew.dashboard.handlers.mcp import _uninstall_scope_files
    from kiro_crew.dashboard.side_readonly_spec import is_readonly_spec_description
    from kiro_crew.mcp_discovery import SCOPE_KIROCREW
    from kiro_crew.mcp_gateway.launch_approval import target_stem
    from kiro_crew.mcp_utils import mcp_server_alias
    from kiro_crew.user_json import loads_user_jsonc

    def declares(servers: Any) -> bool:
        if not isinstance(servers, dict):
            return True
        return any(target_stem(mcp_server_alias(k)) == target_stem(name) for k in servers)

    def derived_copy(stem: str) -> bool:
        return is_native_skill_alias_name(stem) or is_app_agent_launch_name(stem)

    try:
        if declares(_app_owned_mcp_keys()):
            return True
        for label, path in _uninstall_scope_files():
            if label == SCOPE_KIROCREW or label == "agent":
                continue
            if not path.exists():
                continue
            data = loads_user_jsonc(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or declares(data.get("mcpServers", {})):
                return True
        from kiro_crew.agent_discovery import _read_agent_spec

        for directory, generated in (
            (kiro_agents_dir(), OWNED_KIRO_AGENT_FILES),
            (mate_agents_dir(), MATE_SERVICE_AGENT_FILENAMES),
        ):
            try:
                paths = iter_agent_spec_files_strict(directory)
            except FileNotFoundError:
                continue
            for path in paths:
                if path.name in generated or derived_copy(path.stem):
                    continue
                spec = _read_agent_spec(path, operation="mcp_card_share", source="dashboard")
                if spec is None:
                    return True
                if is_readonly_spec_description(spec.get("description")):
                    continue
                if declares(spec.get("mcpServers", {})):
                    return True
    except Exception:  # noqa: BLE001 - unknown sources cannot grant sharing
        return True
    return False


def _sharing_state(name: str) -> dict[str, Any]:
    """Whether MCP server sharing is on, whether *name* is shared, and its answer."""
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        gw = KiroCrewConfig.load().mcp_gateway
    except Exception:  # noqa: BLE001 - an unreadable config offers no sharing
        return {"sharing_on": False, "shared": False, "cwd_use": None}
    answer = (gw.cwd_use or {}).get(name)
    return {
        "sharing_on": bool(gw.enabled),
        "shared": name in set(gw.stub_servers or ()),
        "cwd_use": answer if answer in ("uses_cwd", "not_used") else None,
    }


#: Why :func:`project_place` found no file to write.
NO_CHAT_FOLDER = "no_chat_folder"
HARNESS_NOT_SUPPORTED = "harness_not_supported"
SPEC_SKIPS_PROJECT_FILE = "spec_skips_project_file"

#: What the agent is told for each, with the way forward. A card is never raised
#: for a file the chat would not load, so a project add never reports a server
#: that does not start.
_PLACE_REFUSALS = {
    NO_CHAT_FOLDER: "this chat has no folder to add the server to; use target 'crew'",
    HARNESS_NOT_SUPPORTED: (
        "Kiro Crew cannot confirm this chat's harness loads a project MCP file; "
        "use target 'crew'"
    ),
    SPEC_SKIPS_PROJECT_FILE: (
        "this chat's agent does not load project MCP files (includeMcpJson is off); "
        "use target 'crew'"
    ),
}


def project_place(state: Any, sk: str) -> dict[str, str] | str:
    """The folder and file a ``project`` add from session *sk* writes, or why none.

    Read from the asking chat's live session, never from the agent's body: its
    working folder, and the project MCP file its harness reads.
    :data:`NO_CHAT_FOLDER` when the session has no live provider or its folder is
    not an absolute path; :data:`SPEC_SKIPS_PROJECT_FILE` for a Kiro or KAS chat
    whose agent spec does not opt in with ``includeMcpJson`` (the default agent),
    so a server written there would never load; :data:`HARNESS_NOT_SUPPORTED` for
    every other harness. Claude Code is one of them: whether its session loads
    ``.mcp.json`` depends on the session's setting sources and
    ``strictMcpConfig`` (a Bot's worker sets it) and on the project-server
    approval, none of which Crew reads back from a live session.
    """
    from kiro_crew.agent_sdk.capabilities import capabilities_of
    from kiro_crew.agent_sdk.mcp_json_reach import spec_opts_into_mcp_json
    from kiro_crew.project_mcp_files import KIRO_PROJECT_MCP_FILE

    get_provider = getattr(getattr(state, "sessions", None), "get_provider", None)
    provider = get_provider(sk) if callable(get_provider) else None
    folder = str(getattr(provider, "cwd", "") or "")
    if provider is None or not os.path.isabs(folder):
        return NO_CHAT_FOLDER
    backend = capabilities_of(provider).backend
    if not spec_opts_into_mcp_json(backend):
        return HARNESS_NOT_SUPPORTED
    if not _spec_loads_mcp_json(provider, backend, folder):
        return SPEC_SKIPS_PROJECT_FILE
    return {PARAM_PROJECT_FOLDER: folder, PARAM_PROJECT_FILE: KIRO_PROJECT_MCP_FILE}


def _spec_loads_mcp_json(provider: Any, backend: str, folder: str) -> bool:
    """Whether the agent spec *provider* runs asks *backend* to load ``mcp.json`` files.

    Blocking. ``AcpProvider.client`` is an ``AcpClient`` or an
    ``AcpSessionProvider``; both name their agent as ``_agent``. An unknown agent
    answers False, so no card is raised for a file the chat might not load.
    """
    from kiro_crew.agent_sdk.mcp_json_reach import agent_includes_mcp_json

    client = getattr(provider, "client", provider)
    agent = getattr(client, "_agent", "")
    if not isinstance(agent, str) or not agent:
        return False
    return agent_includes_mcp_json(agent, backend, folder)


async def request_page_change(
    request: web.Request, slot_key: str, sk: str, body: dict[str, Any]
) -> web.Response:
    """Raise the ``mcp.server`` card for a verified caller's ``request_server`` body."""
    from kiro_crew.dashboard.handlers.change_cards import propose_for_caller

    operation = "cards.request_server"
    action = body.get("action", ACTION_ADD)
    if action not in ACTIONS:
        _audit(sk, operation, "denied", "invalid_action")
        return _deny(400, "invalid_action", "action must be add, change or remove")
    if body.get("registry_id") is not None:
        _audit(sk, operation, "denied", "invalid_body")
        return _deny(400, "invalid_body", "registry_id is only for source 'registry'")
    params: dict[str, Any] = {
        "action": action,
        "server": body.get("server"),
        OPTION_USE_NOW: True,
    }
    for key in ("entry", PARAM_USES_CHAT_FOLDER, PARAM_TARGET):
        if key in body:
            params[key] = body[key]
    if body.get(PARAM_TARGET) == TARGET_PROJECT and action == ACTION_ADD:
        place = await asyncio.to_thread(project_place, request.app["state"], sk)
        if isinstance(place, str):
            _audit(sk, operation, "denied", place)
            return _deny(409, place, _PLACE_REFUSALS[place])
        params.update(place)
    return await propose_for_caller(
        request, slot_key, sk, kind=KIND_MCP_SERVER, params=params, reason=body.get("reason")
    )
