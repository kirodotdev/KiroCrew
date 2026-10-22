"""A crewmate's capability catalog and its additions (``kirocrew-mate``).

The agent half of two ``kirocrew-mate`` tools, under the strict
``/api/member/self`` prefix like the rest of that server:

* ``POST /api/member/self/capabilities`` (``find_capabilities``) reads, for the
  crewmate whose own thread the verified caller is, what its worker already has,
  what this gateway has loaded that the worker lacks, and -- given a ``query`` --
  what the MCP registry offers, through the same search the MCP page runs
  (:func:`handlers.mcp_discover.search_registry`). Data only; a read, so any
  turn in the crewmate's own thread may ask.
* :func:`additions_to_draft` turns the ``additions`` a ``request_capabilities``
  draft names into the operations of a ``crewmate.capabilities`` card
  (:mod:`handlers.member_self`). The card applies them to the crewmate's own
  file in place (:mod:`kiro_crew.mate_capabilities`, optimistic lock); a
  registry server is installed by that write, into the crewmate's file and never
  the user's MCP config. Nothing changes until the user presses the card, and
  the worker picks the change up at its next spawn.

Only the crewmate's own chat -- its conductor -- reaches either: a worker
session is not a crewmate thread, and its spec leaves both tools out
(``mate_worker.MATE_WORKER_EXCLUDED_TOOLS``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from aiohttp import web

from kiro_crew import mate_capability_catalog as catalog_mod
from kiro_crew.dashboard.guide_runs import GuideError
from kiro_crew.dashboard.handlers.guide import _audit, _deny, _json_body, _refusal

logger = logging.getLogger(__name__)

#: ``find_capabilities`` operation name (a read: not a user-turn operation).
OP_FIND_CAPABILITIES = "member.find_capabilities"

_REGISTRY_LIMIT = 10
_QUERY_MAX = 200
#: A tool name the card may list as running without asking.
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _service(app: Any) -> Any:
    from kiro_crew.dashboard.change_cards import _capability_service

    return _capability_service(app)


def _is_mate(member: str) -> bool:
    from kiro_crew import mate_capabilities

    return mate_capabilities.binding_is_mate(member)


def _no_file() -> web.Response:
    return _deny(
        409,
        "no_crewmate_file",
        "this crewmate has no worker settings of its own yet, so nothing can be added to it",
    )


def gateway_matches(params: dict[str, Any]) -> set[str]:
    """The servers a capabilities card sets to exactly the entry this gateway runs.

    What lets the card say a server comes from this gateway: its draft value is
    the gateway's own entry, re-read now (``_find_server_spec_anywhere``, the MCP
    page's lookup), enabled, and equal key for key to what a file takes from it
    (``transport_for_file``: command, args, env, headers, cwd and every other key).
    A changed ``env`` or ``cwd``, or an entry the user switched off, is not this
    gateway's server. Blocking.
    """
    from kiro_crew.dashboard.handlers.mcp import _find_server_spec_anywhere

    out: set[str] = set()
    for op in (params.get("draft") or {}).get("operations") or []:
        value = op.get("value")
        if op.get("section") != "mcpServers" or not isinstance(value, dict):
            continue
        name = str(op.get("id") or "")
        current = _find_server_spec_anywhere(name) if name else None
        if (
            isinstance(current, dict)
            and _switched_on(name)
            and catalog_mod.transport_for_file(current) == value
        ):
            out.add(name)
    return out


#: Transport keys whose values may hold a credential (an API key in env, a token in headers).
_SETTINGS_KEYS = ("env", "headers")


def _carries_settings(transport: dict[str, Any] | None) -> bool:
    """Whether a gateway server's transport may hold a credential.

    It sets any env or header value, or the masking every capability view uses
    (``agent_capabilities.safe_view``) would hide part of it: a credential
    passed as an argument (``--api-key <value>``, ``--token=<value>``,
    ``-e NAME=<value>``) is caught there.
    """
    if not isinstance(transport, dict):
        return False
    if any(transport.get(k) for k in _SETTINGS_KEYS):
        return True
    from kiro_crew.agent_capabilities import safe_view

    return safe_view(transport) != transport


def _switched_on(name: str) -> bool:
    """Whether this gateway lists server *name* and the user has not switched it off.

    The entry lookup strips ``disabled``, so the gateway's own server list
    (``mcp_discovery.list_servers``, the catalog's source) answers it. Blocking.
    """
    from kiro_crew.mcp_discovery import list_servers

    try:
        rows = list_servers()
    except Exception:  # noqa: BLE001 - an unreadable config runs no server
        logger.warning("MCP config unreadable; %s counts as not running", name, exc_info=True)
        return False
    return any(
        getattr(row, "name", "") == name and getattr(row, "status", "") != "disabled"
        for row in rows
    )


async def api_member_self_capabilities(request: web.Request) -> web.Response:
    """POST /api/member/self/capabilities — ``{"query"?: text}`` → the catalog.

    Answers ``worker`` (what the crewmate's worker has), ``gateway`` (servers
    and skills loaded here that the worker lacks) and, for a ``query`` of two
    or more characters, ``registry`` (MCP registry results, redacted), plus
    ``next`` saying how to ask for one.
    """
    from kiro_crew import crewmate_dynamic_capabilities
    from kiro_crew.dashboard.handlers.mcp_discover import search_registry
    from kiro_crew.dashboard.handlers.member_self import _caller_member

    try:
        slot_key, sk = await _resolve(request)
        body = await _json_body(request)
    except GuideError as exc:
        return _refusal(exc)
    who = _caller_member(request, slot_key)
    if who is None:
        _audit(sk, OP_FIND_CAPABILITIES, "denied", "not a crewmate thread")
        return _deny(403, "not_a_crewmate", "only a crewmate, in its own chat, can read this")
    member, _store = who
    if not await asyncio.to_thread(crewmate_dynamic_capabilities.enabled_for, member):
        # The crewmate-driven capability path, this crewmate's own preview
        # (``kiro_crew.crewmate_dynamic_capabilities``), read on every call, so a
        # Turn on takes effect at once even in a running session.
        _audit(sk, OP_FIND_CAPABILITIES, "denied", "dynamic_capabilities_off")
        return _deny(
            403,
            "dynamic_capabilities_off",
            "Dynamic capabilities (a Preview) is off for you, so the catalog is not "
            "read; ask with request_capabilities, which offers the user the Preview",
        )
    query = body.get("query", "")
    if not isinstance(query, str) or len(query) > _QUERY_MAX:
        return _deny(400, "invalid_query", f"'query' is text of at most {_QUERY_MAX} characters")
    if not await asyncio.to_thread(_is_mate, member):
        return _no_file()
    service = _service(request.app)
    catalog = await asyncio.to_thread(service._mate_catalog, member)
    spec = await asyncio.to_thread(catalog_mod.worker_spec, member)
    answer: dict[str, Any] = {
        "worker": catalog_mod.worker_has(spec, catalog),
        "gateway": await asyncio.to_thread(catalog_mod.gateway_lacks, spec, catalog),
    }
    query = query.strip()
    if query:
        found = await search_registry(query, limit=_REGISTRY_LIMIT)
        answer["registry"] = [
            {
                k: row.get(k)
                for k in ("id", "title", "description", "provider", "version", "installed")
            }
            for row in found["results"]
        ]
    answer["next"] = (
        "To ask the user for one, call request_capabilities with draft "
        '{"additions": [{"server": <name>, "source": "gateway"} or {"server": <name>, '
        '"source": "registry", "registry_id": <id>} or {"skill": <id>}], each server '
        'with an optional "auto_approve": [<tool>]}. Once the user applies the card, your worker that is already running is reloaded with it, conversation kept; send it the work again.'
    )
    _audit(sk, OP_FIND_CAPABILITIES, "ok")
    return web.json_response(answer)


async def _resolve(request: web.Request) -> tuple[str, str]:
    from kiro_crew.dashboard.handlers.guide import _resolve_agent_caller

    return await _resolve_agent_caller(request, OP_FIND_CAPABILITIES)


def _addition_list(draft: dict[str, Any]) -> list[dict[str, Any]]:
    raw = draft.get("additions")
    if (
        not isinstance(raw, list)
        or not raw
        or len(raw) > catalog_mod.MAX_ADDITIONS
        or not all(isinstance(a, dict) for a in raw)
    ):
        raise catalog_mod.AdditionError(
            "invalid_additions",
            f"'additions' is a list of 1 to {catalog_mod.MAX_ADDITIONS} objects",
        )
    return raw


def _auto_approve(addition: dict[str, Any]) -> list[str]:
    tools = addition.get("auto_approve", [])
    if not isinstance(tools, list) or not all(
        isinstance(t, str) and _TOOL_NAME.fullmatch(t) for t in tools
    ):
        raise catalog_mod.AdditionError(
            "invalid_additions", "'auto_approve' is a list of the server's tool names"
        )
    return list(dict.fromkeys(tools))


async def additions_to_draft(
    app: Any, member: str, draft: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """``(draft, origins)`` for a capabilities card from the additions *draft* names.

    Each ``{"server": name, "source": "gateway"}`` copies this gateway's own
    entry for that server; ``{"server": name, "source": "registry",
    "registry_id": id}`` takes the official registry's install plan, the spec
    ``/api/mcp/discover/install`` would write, under the name that install would
    give it; ``{"skill": id}`` adds a skill this gateway has. ``auto_approve``
    names the tools the card lists as running without asking. Anything the
    worker already has, a name the gateway already uses for another server, and
    a provider other than the official registry (whose install runs outside the
    crewmate's file) are refused with :class:`mate_capability_catalog.AdditionError`.
    """
    from kiro_crew.dashboard.handlers.mcp import _find_server_spec_anywhere
    from kiro_crew.dashboard.handlers.mcp_discover import (
        OfficialPlanError,
        official_install_plan,
    )

    additions = _addition_list(draft)
    if set(draft) - {"additions", "enroll"}:
        # A card of additions shows one plain line per addition and folds the
        # raw rows under Details; a raw operation riding along would be folded
        # there too, unseen. Raw operations go on their own card.
        raise catalog_mod.AdditionError(
            "invalid_additions",
            "a draft of additions takes only additions and enroll; send other "
            "operations as their own request",
        )
    if not await asyncio.to_thread(_is_mate, member):
        raise catalog_mod.AdditionError(
            "no_crewmate_file",
            "this crewmate has no worker settings of its own yet, so nothing can be added",
        )
    catalog = await asyncio.to_thread(_service(app)._mate_catalog, member)
    spec = await asyncio.to_thread(catalog_mod.worker_spec, member)
    has = catalog_mod.worker_has(spec, catalog)
    have_servers = {s["name"] for s in has["servers"]}
    have_skills = set(has["skills"])
    ops: list[dict[str, Any]] = []
    origins: dict[str, dict[str, str]] = {}
    for addition in additions:
        if set(addition) == {"skill"}:
            skill = addition["skill"]
            if not isinstance(skill, str) or skill not in catalog:
                raise catalog_mod.AdditionError(
                    "unknown_skill", "no skill of that id is loaded here"
                )
            if skill in have_skills:
                raise catalog_mod.AdditionError("already_has", f"the worker already has {skill}")
            ops.append(catalog_mod.skill_operation(skill))
            continue
        if set(addition) - {"server", "source", "registry_id", "auto_approve"}:
            raise catalog_mod.AdditionError(
                "invalid_additions", "an addition is {server, source, registry_id?, auto_approve?}"
            )
        name, source = addition.get("server"), addition.get("source")
        tools = _auto_approve(addition)
        if source == catalog_mod.SOURCE_GATEWAY:
            if not isinstance(name, str) or not name:
                raise catalog_mod.AdditionError("invalid_additions", "'server' names a server")
            if name in have_servers:
                raise catalog_mod.AdditionError("already_has", f"the worker already has {name}")
            entry = await asyncio.to_thread(_find_server_spec_anywhere, name)
            if not isinstance(entry, dict) or not await asyncio.to_thread(_switched_on, name):
                raise catalog_mod.AdditionError(
                    "not_on_gateway",
                    f"this gateway runs no MCP server named {name} (absent or switched off)",
                )
            transport = catalog_mod.transport_for_file(entry)
            if _carries_settings(transport):
                # A card's params and plan reach the proposing crewmate, and a
                # credential in env, headers or arguments would go with them.
                raise catalog_mod.AdditionError(
                    "carries_settings",
                    f"{name} carries its own env, headers or credential arguments, so "
                    "it is not copied onto a card; tell the user to add it in your advanced "
                    "settings",
                )
            origin = {"source": catalog_mod.SOURCE_GATEWAY}
        elif source == catalog_mod.SOURCE_REGISTRY:
            rid = addition.get("registry_id")
            if not isinstance(rid, str) or not rid.strip() or len(rid) > 200:
                raise catalog_mod.AdditionError(
                    "invalid_additions", "a registry addition names its registry_id"
                )
            try:
                name, plan, _method, _env = await official_install_plan(rid.strip())
            except OfficialPlanError as exc:
                raise catalog_mod.AdditionError("registry_unavailable", exc.message) from None
            if await asyncio.to_thread(_find_server_spec_anywhere, name) is not None:
                raise catalog_mod.AdditionError(
                    "name_in_use",
                    f"this gateway already runs a server named {name}; add it from the gateway",
                )
            transport = catalog_mod.transport_for_file(plan)
            origin = {"source": catalog_mod.SOURCE_REGISTRY, "registry_id": rid.strip()}
        else:
            raise catalog_mod.AdditionError(
                "invalid_additions", "'source' is gateway or registry (the official registry)"
            )
        if name in have_servers:
            raise catalog_mod.AdditionError("already_has", f"the worker already has {name}")
        ops.extend(catalog_mod.addition_operations(name, transport, tools))
        origins[name] = origin
    out: dict[str, Any] = {"operations": ops}
    if "enroll" in draft:
        out["enroll"] = draft["enroll"]
    return out, origins
