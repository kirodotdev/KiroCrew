"""Jev's range: ``POST /api/chat/slots/{slot}/jev-range``."""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers._shared import read_bounded_json, require_owner_dashboard_request
from kiro_crew.dashboard.slot_ownership import deny_app_slot_access, slot_not_found
from kiro_crew.effort import EFFORT_LEVELS
from kiro_crew.llm_helpers_parts.model_fallback import provider_advertised_ids


def _invalid(error: str) -> web.Response:
    return web.json_response({"error": error, "code": "jev_range_invalid"}, status=400)


async def api_chat_slot_jev_range(request: web.Request) -> web.Response:
    """Replace the models and effort span Jev may pick from for this slot.

    Body: {"models": [ids], "effort_min": level | "", "effort_max": level | ""};
    an empty list or "" leaves that side unbounded. Owner-only, like arming
    ``jev_route``: the range decides what the owner's credential may spend on.
    """
    state = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_jev_range")
    if denied is not None:
        return denied
    denied = await require_owner_dashboard_request(request, "chat_slot_jev_range")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    if state._slots.get(name) is not slot:
        return slot_not_found()
    models = body.get("models", [])
    effort_min = body.get("effort_min", "")
    effort_max = body.get("effort_max", "")
    if not isinstance(models, list) or not all(isinstance(m, str) and m for m in models):
        return _invalid("models must be a list of model ids")
    for field, level in (("effort_min", effort_min), ("effort_max", effort_max)):
        if level != "" and level not in EFFORT_LEVELS:
            return _invalid(f"{field} must be one of: {', '.join(EFFORT_LEVELS)}")
    if (
        effort_min
        and effort_max
        and EFFORT_LEVELS.index(effort_min) > EFFORT_LEVELS.index(effort_max)
    ):
        return _invalid("effort_min is above effort_max")
    # Imported here to keep the decisions package off the gateway boot path.
    from kiro_crew.decisions.points.model_route import _in_range

    # A cold provider advertises nothing, so its ids cannot be checked yet.
    advertised = provider_advertised_ids(state.sessions.get_provider(effective_session_key(slot)))
    unknown = [m for m in models if advertised and not _in_range(m, advertised)]
    if unknown:
        return _invalid(f"models not offered by this session: {', '.join(unknown)}")
    slot.jev_range = {
        "models": list(dict.fromkeys(models)),
        "effort_min": effort_min,
        "effort_max": effort_max,
    }
    state.push_slots_update()
    return web.json_response({"ok": True, "jev_range": slot.jev_range})
