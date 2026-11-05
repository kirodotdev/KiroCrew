"""``PUT /api/agent-spec-pins/{agent}``: set ``agent.spec_pins`` from the chat's chooser.

The body ``{"file": "<file name>"}`` pins the name. A pin is checked exactly as
``kirocrew agent pin`` checks it (:func:`kiro_crew.agent_spec_pin_ops.check_pin_target`)
and written under the config file lock; each write is recorded in the SEL. Listing and
clearing pins is the CLI's (``kirocrew agent pin`` / ``--clear``).

The route is owner-only. A pin decides which installed spec, with its tools and
prompt, every later session of that agent runs, so it is a machine-global change of
the same weight as editing an agent; the routes sit behind the dashboard's token
authentication and the owner gate, and a refused caller gets the audited 403.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew import agent_spec_pin_ops, agent_spec_pins
from kiro_crew.config.loader_document import ConfigReadError
from kiro_crew.dashboard.chat_utils_awaits import run_config_write
from kiro_crew.dashboard.handlers._shared import (
    guard_owner_surface_routes,
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.sel import sel as _sel

logger = logging.getLogger(__name__)


def _caller(request: web.Request) -> str:
    return str(request.get("user") or "dashboard")


def _refusal(exc: agent_spec_pin_ops.PinRefused) -> web.Response:
    status = 404 if exc.code == "file_not_found" else 400
    if exc.code == "agents_dir_unreadable":
        status = 503
    elif exc.code == "pin_overlay_owned":
        status = 409
    return web.json_response({"error": str(exc), "code": exc.code}, status=status)


async def api_agent_spec_pin_write(request: web.Request) -> web.Response:
    """PUT /api/agent-spec-pins/{agent} -- pin *agent* to ``body.file``."""
    denied = await require_owner_dashboard_request(request, "agent_spec_pin.write")
    if denied is not None:
        return denied
    agent = request.match_info.get("agent", "")
    if not agent_spec_pins.valid_pin_agent_name(agent):
        return web.json_response(
            {"error": "invalid agent name", "code": "invalid_agent_name"}, status=400
        )
    # The default cap: the body is one short field.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None
    file_name = body.get("file")
    if not isinstance(file_name, str):
        return web.json_response(
            {"error": "file must be a string", "code": "invalid_file_name"}, status=400
        )
    try:
        await asyncio.to_thread(
            agent_spec_pin_ops.check_pin_target, agent, file_name, source="dashboard"
        )
    except agent_spec_pin_ops.PinRefused as exc:
        return _refusal(exc)
    try:
        existed = await run_config_write(agent_spec_pin_ops.write_spec_pin, agent, file_name)
    except agent_spec_pin_ops.PinRefused as exc:
        return _refusal(exc)
    except ConfigReadError:
        logger.exception("Refusing to write an agent spec pin: config unreadable")
        return web.json_response(
            {"error": "failed to read config file", "code": "config_unreadable"}, status=500
        )
    agent_spec_pin_ops.audit_pin_write(
        _sel().log_api_access,
        caller=_caller(request),
        source="dashboard",
        agent=agent,
        file_name=file_name,
    )
    return web.json_response(
        {"ok": True, "agent": agent, "file": file_name, "previous_pin_existed": existed}
    )


# Owner surfaces: no route here is scoped to a member.
guard_owner_surface_routes(globals(), member_scoped=frozenset(), dashboard_owner_only=True)
