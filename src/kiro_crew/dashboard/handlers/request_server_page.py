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

from typing import Any

from aiohttp import web

from kiro_crew.change_card_mcp_server import (
    ACTION_ADD,
    ACTIONS,
    KIND_MCP_SERVER,
    OPTION_USE_NOW,
    PARAM_USES_CHAT_FOLDER,
)
from kiro_crew.dashboard.handlers.guide import _audit, _deny

#: The request names this flow, as opposed to a template addition.
SOURCE_CUSTOM = "custom"


def is_page_request(body: dict[str, Any]) -> bool:
    """Whether a ``request_server`` body asks about a server on the MCP page."""
    action = body.get("action", ACTION_ADD)
    return action != ACTION_ADD or body.get("source") == SOURCE_CUSTOM


def server_state(name: str) -> dict[str, Any]:
    """The card's snapshot of server *name*. Blocking.

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
    for key in ("entry", PARAM_USES_CHAT_FOLDER):
        if key in body:
            params[key] = body[key]
    return await propose_for_caller(
        request, slot_key, sk, kind=KIND_MCP_SERVER, params=params, reason=body.get("reason")
    )
