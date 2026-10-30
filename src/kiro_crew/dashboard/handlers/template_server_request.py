"""A template-based session asks for an MCP server (``request_server``), and the card that adds it.

Three routes, split the way the change cards are:

* ``POST /api/cards/agent/request-server`` -- the agent half, MCP-only under the
  strict ``/api/cards/agent`` prefix. The session it acts for is the verified
  caller's own (:func:`handlers.guide._resolve_agent_caller`, a user-turn
  operation), never a name from the body, and so is the template: the one that
  session runs on, read from its execution record. A cron, a subagent, a
  messaging channel, a patrol or any other turn the user did not send is
  refused with the reason, so no card waits where nobody sees it. A crewmate
  asks with ``request_capabilities`` instead. The server is resolved by the
  gateway (:func:`member_capability_catalog.resolve_server`): a server this
  gateway already runs, or the official registry's install plan -- never a
  launch line from the request. On a backend where an addition cannot reach the
  template's sessions (:func:`template_mcp_additions.addition_support`) the
  request is refused with that reason. The card is proposed through
  :func:`handlers.change_cards.propose_for_caller`, the body ``propose_change``
  runs, and only the owner's browser can apply it.
* ``POST /api/agents/detail/{name}/mcp-additions`` and
  ``DELETE /api/agents/detail/{name}/mcp-additions/{server}`` -- the browser half,
  owner-only cookie auth, hooked by the card engine. The POST is a card step
  only (it records the card it ran under); the DELETE is that card's Undo, and
  with a ``card_id`` removes the addition only when that card recorded it.

After either write the agent specs are rebuilt (the installers merge the record)
and running sessions pick the change up live where every live process can
(``mcp_hot_reload``), else they are reset, exactly as an MCP page change.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from aiohttp import web

from kiro_crew import template_mcp_additions as _additions
from kiro_crew.dashboard.guide_runs import GuideError
from kiro_crew.dashboard.handlers.guide import _audit, _deny, _json_body, _resolve_agent_caller

logger = logging.getLogger(__name__)

#: ``request_server`` operation name, in :data:`handlers.guide._USER_TURN_OPERATIONS`.
OP_REQUEST_SERVER = "cards.request_server"

_BODY_KEYS = frozenset({"server", "source", "registry_id", "reason"})

#: What each caller refusal tells the agent: the reason, in this tool's words, so
#: it can tell the user plainly instead of retrying from a turn nobody sees.
_CALLER_REFUSALS: dict[str, str] = {
    "subagent_caller": (
        "a subagent cannot raise a card; tell your parent session which server the "
        "work needs and let it ask"
    ),
    "unattended_caller": (
        "a scheduled run has nobody watching to approve a card; say in your result "
        "which server the work needs"
    ),
    "channel_caller": (
        "this message came from a messaging channel, and a card is approved only in "
        "the dashboard; tell the user which server the work needs"
    ),
    "not_user_turn": (
        "only a message the user sent in the dashboard can raise this card, not a "
        "patrol, a wake, a schedule or another session's message; say which server "
        "the work needs and ask when the user writes next"
    ),
    "no_dashboard_turn": "this request did not come from a message sent in the dashboard",
    "no_live_slot": "this session is not open in a dashboard tab, so nobody would see the card",
}


def _caller_refusal(exc: GuideError) -> web.Response:
    message = _CALLER_REFUSALS.get(exc.code, exc.message.replace("guide", "card"))
    status = exc.status if exc.status in (400, 403, 404, 409, 429) else 400
    return _deny(status, exc.code, message)


# ── who is asking, and for which template ──


def _caller_template(state: Any, slot_key: str, sk: str) -> tuple[str, str, str]:
    """``(template, code, message)``: the template the caller runs on, or why there is none.

    Blocking (reads the session's execution record).
    """
    from kiro_crew import mate_worker, members
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.mate_thread_wakes import is_crewmate_thread
    from kiro_crew.execution_context import read_session_execution

    slot = state.get_slot(slot_key)
    if (
        is_crewmate_thread(slot)
        or members.is_crewmate_worker_session(slot_key)
        or members.is_crewmate_worker_session(sk)
    ):
        return (
            "",
            "crewmate_caller",
            "a crewmate asks for its worker's servers with request_capabilities",
        )
    try:
        execution = read_session_execution(slot_history_key(slot))
    except Exception:  # noqa: BLE001 - an unreadable record names no template
        logger.debug("request_server: execution unreadable for %s", slot_key, exc_info=True)
        execution = None
    template = getattr(execution, "template_id", "") if execution is not None else ""
    if not isinstance(template, str) or not template:
        return (
            "",
            "no_template",
            "this session runs on no agent template a server can be added to",
        )
    if mate_worker.is_mate_owned_agent(template):
        return (
            "",
            "crewmate_caller",
            "a crewmate asks for its worker's servers with request_capabilities",
        )
    return template, "", ""


async def _caller_backend(state: Any, sk: str) -> str:
    """The backend the caller's turn runs on (``""`` is Kiro's own id).

    A live provider registered under the key names its own backend; otherwise the
    member-aware selector answers, the same one a new provider is built through.
    """
    from kiro_crew.agent_sdk.capabilities import SessionCapabilities
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.crewmate_harness import crewmate_backend_pin
    from kiro_crew.members import select_provider_backend

    try:
        provider = state.sessions.get_provider(sk)
        caps = getattr(provider, "capabilities", None)
        if isinstance(caps, SessionCapabilities) and caps.backend is not None:
            return str(caps.backend)
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        # A crewmate's own pin is checked first (H17); crewmate callers are refused
        # before this, so a pin that cannot start reads as the default harness.
        backend = select_provider_backend(
            sk,
            cfg.agent.member_acp_backend,
            cfg.agent.acp_backend,
            crew_backend=crewmate_backend_pin(cfg, sk),
        )
    except Exception:  # noqa: BLE001 - unknown is treated as the default harness
        logger.debug("request_server: backend unreadable for %s", sk, exc_info=True)
        backend = ""
    return backend or ""


# ── reach: which templates, sessions and members one addition reaches ──


def addition_reach_view(state: Any, template: str) -> dict[str, Any]:
    """The templates, open sessions and crew members an addition to *template* reaches.

    Templates come from :func:`template_mcp_additions.addition_reach`; each open
    dashboard session is placed by its own execution record. Blocking.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.template_mcp_additions import addition_reach

    templates = list(addition_reach(template))
    wanted = set(templates)
    sessions: list[str] = []
    for slot in list(getattr(state, "_slots", {}).values()):
        try:
            execution = read_session_execution(slot_history_key(slot))
        except Exception:  # noqa: BLE001 - an unplaceable session is not listed
            continue
        if execution is not None and getattr(execution, "template_id", "") in wanted:
            sessions.append(str(getattr(slot, "title", "") or getattr(slot, "key", "")))
    try:
        agents = KiroCrewConfig.load().agents
    except Exception:  # noqa: BLE001 - the card still lists templates and sessions
        agents = {}
    members = sorted(
        str(getattr(cfg, "display_name", "") or crew)
        for crew, cfg in agents.items()
        if getattr(cfg, "kiro_agent", "") in wanted
    )
    aliases = {
        alias: target
        for alias, target in _additions._TEMPLATE_ALIASES.items()
        if alias in templates
    }
    return {
        "templates": templates,
        "aliases": aliases,
        "sessions": sorted(sessions),
        "members": members,
    }


def existing_grants(template: str, server: str) -> list[str]:
    """``allowedTools`` entries for *server* on every template the addition reaches. Blocking.

    The card adds no approval, but a grant the owner wrote earlier on ANY template
    the server will reach (the requested one or its alias, kept across an Undo or
    written by hand) still applies once the server is mounted there, so the card
    names each one, prefixed with its template, rather than promising every call
    asks.
    """
    from kiro_crew.dashboard.handlers.agents import _agent_detail_candidates
    from kiro_crew.template_mcp_additions import addition_reach

    ref = f"@{server}"
    out: list[str] = []
    for name in addition_reach(template) or (template,):
        try:
            candidates = _agent_detail_candidates(name)
        except Exception:  # noqa: BLE001 - display only
            continue
        for _path, spec in candidates:
            allowed = spec.get("allowedTools") if isinstance(spec, dict) else None
            if isinstance(allowed, list):
                out += [
                    f"{name}: {g}"
                    for g in allowed
                    if isinstance(g, str) and (g == ref or g.startswith(ref + "/"))
                ]
    return sorted(dict.fromkeys(out))


def server_tools(name: str) -> list[str]:
    """The tool names this gateway last saw server *name* advertise, else ``[]``."""
    from kiro_crew.mcp_discovery import probe_metadata

    try:
        cached = probe_metadata(name)
    except Exception:  # noqa: BLE001 - display only: the card says it is not listed
        return []
    tools = getattr(cached, "tools", None) or []
    return [str(t) for t in tools if isinstance(t, str)]


def live_apply(state: Any) -> bool:
    """Whether every live process applies an MCP change without a reset. Never marks."""
    from kiro_crew.mcp_hot_reload import live_sessions_apply_mcp_live

    try:
        sessions = state.sessions
        providers = list(sessions.active_providers()) + list(sessions.warm_providers())
        return bool(live_sessions_apply_mcp_live(providers))
    except Exception:  # noqa: BLE001 - fail closed: the card says sessions restart
        return False


def template_declares(template: str, server: str) -> bool:
    """Whether *template*'s own spec already declares *server* (not via an addition). Blocking."""
    from kiro_crew import template_mcp_additions as additions
    from kiro_crew.dashboard.handlers.agents import _agent_detail_candidates

    if server in additions.read_additions(template):
        return False
    try:
        candidates = _agent_detail_candidates(template)
    except Exception:  # noqa: BLE001 - unreadable: the record's add-only merge still holds
        return False
    for _path, spec in candidates:
        servers = spec.get("mcpServers") if isinstance(spec, dict) else None
        if isinstance(servers, dict) and server in servers:
            return True
    return False


# ── agent half ──


async def api_cards_agent_request_server(request: web.Request) -> web.Response:
    """POST /api/cards/agent/request-server — ``{server?, source, registry_id?, reason?}`` → Card."""
    from kiro_crew import template_mcp_additions as additions
    from kiro_crew.change_card_catalog import KIND_TEMPLATE_MCP_SERVER
    from kiro_crew.dashboard.handlers.change_cards import propose_for_caller
    from kiro_crew.dashboard.handlers.member_capability_catalog import resolve_server
    from kiro_crew.mate_capability_catalog import AdditionError as CatalogAdditionError

    try:
        slot_key, sk = await _resolve_agent_caller(request, OP_REQUEST_SERVER)
        body = await _json_body(request)
    except GuideError as exc:
        return _caller_refusal(exc)
    unknown = sorted(set(body) - _BODY_KEYS)
    if unknown:
        # A command, url, template or session is never the caller's to give.
        _audit(sk, OP_REQUEST_SERVER, "denied", "unknown_field")
        return _deny(400, "invalid_body", f"unknown field '{unknown[0][:64]}'")
    state = request.app["state"]
    template, code, message = await asyncio.to_thread(_caller_template, state, slot_key, sk)
    if not template:
        _audit(sk, OP_REQUEST_SERVER, "denied", code)
        return _deny(403 if code == "crewmate_caller" else 409, code, message)
    support = additions.addition_support(template, await _caller_backend(state, sk))
    if not support.supported:
        _audit(sk, OP_REQUEST_SERVER, "denied", "unsupported")
        return _deny(409, "unsupported", support.reason)
    try:
        name, transport, origin = await resolve_server(
            body.get("server"),
            body.get("source"),
            body.get("registry_id"),
            settings_hint="ask the user to add it on the MCP page instead",
        )
    except CatalogAdditionError as exc:
        _audit(sk, OP_REQUEST_SERVER, "denied", exc.code)
        return _deny(400, exc.code, exc.message)
    try:
        if transport is None:
            raise additions.AdditionError("entry_not_object")
        entry = additions.validate_entry(transport)
    except additions.AdditionError as exc:
        _audit(sk, OP_REQUEST_SERVER, "denied", "entry_not_recordable")
        return _deny(
            400,
            "entry_not_recordable",
            f"{name}'s entry cannot be added to an agent ({exc}); ask the user to add it "
            "on the MCP page instead",
        )
    return await propose_for_caller(
        request,
        slot_key,
        sk,
        kind=KIND_TEMPLATE_MCP_SERVER,
        params={"template": template, "server": name, "entry": entry, "origin": origin},
        reason=body.get("reason"),
    )


# ── browser half (card steps) ──


async def _owner_only(request: web.Request, operation: str) -> web.Response | None:
    """403 ``owner_only`` unless this is the owner's own browser (never an internal caller)."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    if request.get("internal_auth") is True:
        _audit(
            str(request.get("user") or "internal"),
            operation,
            "denied",
            "internal caller",
        )
        return _deny(403, "owner_only", "only the dashboard owner can change an agent's servers")
    return await require_owner_dashboard_request(request, operation)


#: The card owns no spec write and no rebuild lock. It writes the record (under
#: the record's own lock) and asks for one ordinary rebuild. Every rebuild, the MCP
#: page's included, merges the record, so a rebuild that raced with an older
#: snapshot is corrected by the next one; the record is the source of truth.


def _rebuild_specs() -> bool:
    """Rewrite the agent specs from the record; whether every spec was written. Blocking.

    A refused write (another data home owns the agents directory) or a conductor
    spec the rebuild held back both leave a spec that still shows the OLD record,
    so either counts as not written, and so does a rebuild that raised.
    """
    from kiro_crew.agent import rebuild_agent_config_reporting

    held: list[bool] = []
    try:
        _path, wrote = rebuild_agent_config_reporting(_held_out=held)
    except Exception:  # noqa: BLE001 - reported to the card as not applied
        logger.warning("agent rebuild after a template MCP addition failed", exc_info=True)
        return False
    return bool(wrote) and not any(held)


def _spec_carries(template: str) -> bool:
    """Whether *template*'s sessions read an addition from a spec the rebuild writes.

    Only an installer-merged template (a conductor, alias resolved) does. Every
    other covered template reads the record itself at spawn
    (``template_mcp_additions.merge_at_spawn``), so a rebuild that is declined (the
    shared-home guard on a non-default data home) leaves nothing stale for it.
    """
    canonical = _additions._TEMPLATE_ALIASES.get(template, template)
    return canonical in _additions.INSTALLER_MERGED_TEMPLATES


def _rebuild_required(template: str) -> bool:
    """Rebuild the specs; whether that failed in a way that leaves *template* stale. Blocking."""
    wrote = _rebuild_specs()
    return not wrote and _spec_carries(template)


def _reconcile_after_rollback() -> None:
    """Rebuild once more after a rollback, so specs a failed rebuild already wrote match the record.

    Best effort: if this rebuild fails too, the next rebuild (gateway start, any MCP
    change) converges every spec on the record, which is the source of truth.
    """
    if not _rebuild_specs():
        logger.warning(
            "template MCP additions: specs still disagree with the record after a rollback; "
            "the next rebuild converges them"
        )


async def _reach_sessions(request: web.Request) -> int:
    """Apply live where every live process can, else reset sessions, as an MCP page change does."""
    from kiro_crew.dashboard.handlers.mcp import _mcp_hot_reload_active

    if _mcp_hot_reload_active(request):
        return 0
    from kiro_crew.dashboard.handlers.sessions import _reset_all_sessions

    return int(await _reset_all_sessions(request))


#: What the card is told when the record changed but a spec could not be rewritten:
#: the record is put back, so the card's state and every spec still agree.
_NOT_REWRITTEN = (
    "the agent's settings file could not be rewritten, so nothing changed; try again, "
    "or check the gateway log"
)


async def api_template_mcp_addition_add(request: web.Request) -> web.Response:
    """POST /api/agents/detail/{name}/mcp-additions — ``{server, entry}``, a card step only."""
    from kiro_crew import template_mcp_additions as additions
    from kiro_crew.dashboard.handlers.change_cards import HEADER_CARD_ID

    operation = "template_mcp_addition_add"
    denied = await _owner_only(request, operation)
    if denied is not None:
        return denied
    card_id = (request.headers.get(HEADER_CARD_ID) or "").strip()
    if not card_id:
        # Not an authorization check (``_owner_only`` above is that): the card id
        # is what the record keeps for Undo, so a write with none could never be
        # taken back by its card.
        return _deny(403, "card_required", "an agent's servers are added from a change card")
    try:
        body = await request.json()
    except Exception:
        return _deny(400, "invalid_json", "invalid JSON body")
    if not isinstance(body, dict) or set(body) != {"server", "entry"}:
        return _deny(400, "invalid_body", "body is {server, entry}")
    template = request.match_info["name"]
    try:
        added = await asyncio.to_thread(
            additions.add_addition,
            template,
            body["server"],
            body["entry"],
            approved_by=str(request.get("user") or "owner"),
            card_id=card_id,
            # Decided under the record lock: another card may have approved this
            # server after this card's own check, and replacing that approval would
            # let this card's rollback or Undo delete it.
            only_if_absent=True,
        )
    except additions.AdditionError as exc:
        return _deny(400, "addition_refused", str(exc))
    except (OSError, ValueError):
        logger.warning("template MCP additions record unwritable", exc_info=True)
        return _deny(503, "record_unavailable", "the agent server record could not be written")
    if added is None:
        return _deny(
            409,
            "already_added",
            "another card added this server to this agent first; nothing changed",
        )
    if await asyncio.to_thread(_rebuild_required, template):
        # Put the record back: a recorded addition no spec carries would read as
        # applied while no session can reach it.
        try:
            await asyncio.to_thread(
                additions.remove_addition, template, added.server, card_id=card_id
            )
        except (additions.AdditionError, OSError, ValueError):
            logger.warning("rolling back template MCP addition failed", exc_info=True)
        # The failed rebuild may already have written some specs with the server:
        # rebuild again from the restored record so no spec keeps it.
        await asyncio.to_thread(_reconcile_after_rollback)
        _audit(str(request.get("user") or "owner"), operation, "error", "spec_not_rewritten")
        return _deny(503, "spec_not_rewritten", _NOT_REWRITTEN)
    _audit(
        str(request.get("user") or "owner"),
        operation,
        "ok",
        f"{template}:{added.server}",
    )
    applied = {"rebuilt": True, "sessions_reset": await _reach_sessions(request)}
    return web.json_response(
        {
            "ok": True,
            "template": template,
            "server": added.server,
            "card_id": card_id,
            **applied,
        }
    )


async def api_template_mcp_addition_remove(request: web.Request) -> web.Response:
    """DELETE /api/agents/detail/{name}/mcp-additions/{server}[?card_id=] — remove one addition."""
    from kiro_crew import template_mcp_additions as additions

    operation = "template_mcp_addition_remove"
    denied = await _owner_only(request, operation)
    if denied is not None:
        return denied
    template = request.match_info["name"]
    server = request.match_info["server"]
    card_id = request.query.get("card_id") or None
    # Kept so a removal whose spec rewrite fails can be put back exactly.
    prior = (await asyncio.to_thread(additions.read_additions, template)).get(server)
    try:
        removed = await asyncio.to_thread(
            additions.remove_addition, template, server, card_id=card_id
        )
    except additions.AdditionError as exc:
        return _deny(400, "addition_refused", str(exc))
    except (OSError, ValueError):
        logger.warning("template MCP additions record unwritable", exc_info=True)
        return _deny(503, "record_unavailable", "the agent server record could not be written")
    if not removed:
        return _deny(
            409,
            "not_found",
            "that server is not added to this agent by this card any more",
        )
    if await asyncio.to_thread(_rebuild_required, template):
        # The spec still mounts the server, so the Undo did not happen: put the
        # record back as it was, and the card keeps its Undo for a retry.
        if prior is not None:
            try:
                # Only while the server is still absent, decided under the
                # record lock: another card may have approved it since.
                await asyncio.to_thread(
                    functools.partial(
                        additions.add_addition,
                        template,
                        server,
                        dict(prior.entry),
                        approved_by=prior.approved_by,
                        card_id=prior.card_id,
                        approved_at=prior.approved_at,
                        only_if_absent=True,
                    )
                )
            except (additions.AdditionError, OSError, ValueError):
                logger.warning("restoring template MCP addition failed", exc_info=True)
        # The failed rebuild may already have dropped the server from some specs:
        # rebuild again from the restored record so every spec mounts it again.
        await asyncio.to_thread(_reconcile_after_rollback)
        _audit(str(request.get("user") or "owner"), operation, "error", "spec_not_rewritten")
        return _deny(503, "spec_not_rewritten", _NOT_REWRITTEN)
    _audit(str(request.get("user") or "owner"), operation, "ok", f"{template}:{server}")
    applied = {"rebuilt": True, "sessions_reset": await _reach_sessions(request)}
    return web.json_response({"ok": True, "template": template, "server": server, **applied})
