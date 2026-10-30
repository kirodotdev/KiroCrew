"""Set the display name and picture shown for a person on the People page.

Any admitted person may set them for anyone, themselves included: everyone has
the same rights here. The record is keyed by the person's stable Tailscale user
id, so it follows a renamed login, and it works for someone who was never
invited. It is display only. Nothing that decides access reads it, and the
verified login stays on the row beside the name.

Each change is written to the security event log as a ``shared_crew_action``
(``person_display``) naming who changed whom and the old and new name, and
open People pages are told to refetch with a ``shared_crew.people`` frame.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard import tailnet_live_allowlist
from kiro_crew.shared_crew import tailscale_invite_store as store
from kiro_crew.shared_crew.audit import EXTRA_DISPLAY_CHANGE, new_action_record, record_action
from kiro_crew.shared_crew.collab import FRAME_PEOPLE
from kiro_crew.shared_crew.contracts import ActionOutcome, ActionType
from kiro_crew.shared_crew.identity import actor_of
from kiro_crew.shared_crew.routes_invites import preview_only

logger = logging.getLogger(__name__)

DISPLAY_PATH = "/api/shared-crew/people/{user_id}/display"
#: The JSON body: an alias and at most one avatar, base64-encoded.
_MAX_BODY_BYTES = (store.MAX_AVATAR_BYTES * 4) // 3 + 4096


def _error(status: int, code: str) -> web.Response:
    return web.json_response({"error": code, "code": code}, status=status)


def _refusal(request: web.Request) -> web.Response | None:
    """Any admitted dashboard person; never an app token or a restricted session."""
    from kiro_crew.dashboard.handlers._shared import _is_restricted_session

    if request.get("is_dashboard_user") is not True or request.get("app"):
        return _error(403, "forbidden")
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        return _error(403, "restricted_session")
    return None


def login_of(user_id: str) -> str:
    """The verified login this Crew knows for *user_id*, or ``""``."""
    state = store.load_state()
    for table in ("members", "display"):
        row = state.get(table, {}).get(user_id)
        if isinstance(row, dict) and row.get("login"):
            return str(row["login"]).strip().lower()
    known = sorted(tailnet_live_allowlist.logins_for(user_id))
    return known[0] if known else ""


async def _read_body(request: web.Request) -> dict[str, Any] | None:
    if request.content_length is not None and request.content_length > _MAX_BODY_BYTES:
        return None
    chunks: list[bytes] = []
    total = 0
    while total <= _MAX_BODY_BYTES:
        chunk = await request.content.read(_MAX_BODY_BYTES + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > _MAX_BODY_BYTES:
        return None
    try:
        body = json.loads(b"".join(chunks) or b"null")
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


@preview_only
async def api_set_display(request: web.Request) -> web.Response:
    refused = _refusal(request)
    if refused is not None:
        return refused
    user_id = request.match_info["user_id"]
    if not store.valid_user_id(user_id):
        return _error(400, "bad_user_id")
    login = await asyncio.to_thread(login_of, user_id)
    if not login:
        return _error(404, "unknown_person")
    body = await _read_body(request)
    if body is None:
        return _error(413 if (request.content_length or 0) > _MAX_BODY_BYTES else 400, "bad_request")
    alias = store.clean_alias(body.get("alias"))
    current = (await asyncio.to_thread(store.display_overrides)).get(user_id) or {}
    if not current:
        member = (await asyncio.to_thread(store.load_state)).get("members", {}).get(user_id)
        current = member if isinstance(member, dict) else {}
    avatar_id = str(current.get("avatar_id") or "")
    if body.get("clear_avatar") is True:
        avatar_id = ""
    encoded = body.get("avatar_data_base64")
    if isinstance(encoded, str) and encoded:
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return _error(400, "bad_request")
        try:
            avatar_id = await asyncio.to_thread(store.save_avatar, data)
        except ValueError as exc:
            return _error(413 if str(exc) == "avatar_too_large" else 400, str(exc))
    actor = actor_of(request)
    before, after = await asyncio.to_thread(
        lambda: store.set_display(
            user_id, login=login, alias=alias, avatar_id=avatar_id, actor=actor.principal.id
        )
    )
    record = new_action_record(
        ActionType.PERSON_DISPLAY,
        session_key="",
        initiator=actor,
        outcome=ActionOutcome.APPLIED,
        target=f"person:{user_id}",
        extra={
            EXTRA_DISPLAY_CHANGE: {
                "user_id": user_id,
                "login": login,
                "old_name": before["alias"],
                "new_name": after["alias"],
                "avatar_changed": before["avatar_id"] != after["avatar_id"],
            }
        },
    )
    audited = await record_action(record)
    state = request.app.get("state")
    if state is not None:
        try:
            state.broadcast_ws(FRAME_PEOPLE, {"type": FRAME_PEOPLE, "user_id": user_id})
        except Exception:  # noqa: BLE001 -- a page that misses it refetches on its own
            logger.debug("people frame broadcast failed", exc_info=True)
    return web.json_response(
        {"user_id": user_id, "alias": alias, "avatar_id": avatar_id, "audited": audited}
    )


def register(app: web.Application) -> None:
    """Register the display route on *app*."""
    app.router.add_put(DISPLAY_PATH, api_set_display)
