"""Mood Music — backend API routes (app storage only).

Registered at gateway startup through ``BUILTIN_NAMES``. Every route refuses
while the app is disabled and is owner-only from the dashboard, reads included;
an app token keeps the access its manifest grant gives it.

  GET  /api/apps/mood-music/state               -> moments, queue, feedback
  POST /api/apps/mood-music/moments             -> record a mood moment
  POST /api/apps/mood-music/queue               -> replace the queue with new picks
  PUT  /api/apps/mood-music/queue/{id}/feedback -> like / skip / clear a pick

A pick's link is checked in ``links.py``: a YouTube pick is stored as its video
id alone, a bilibili pick as an https URL on an exact bilibili host. A request
carrying one bad pick is refused whole, so nothing half-checked is stored.
"""

from __future__ import annotations

import asyncio
import json
import re
from functools import wraps
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.mood_music.backend import links
from kiro_crew.apps.builtins.mood_music.backend.store import MAX_QUEUE, VERDICTS, MoodStore
from kiro_crew.apps.manager import app_data_dir, is_app_enabled
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

APP_NAME = "mood-music"
_PREFIX = f"/api/apps/{APP_NAME}"

_MAX_MOOD = 200
_MAX_NOTE = 1000
_MAX_TEXT = 200
_MAX_WHY = 400
_MAX_TAGS = 8
_MAX_TAG = 40
_MAX_SOURCE = 128
_ID_RE = re.compile(r"[0-9a-f]{32}")


def _store() -> MoodStore:
    """The store under the ACTIVE data home, resolved per call (KIROCREW_HOME)."""
    return MoodStore(app_data_dir(APP_NAME) / "state.json")


def _bad_request(error: str, code: str) -> web.Response:
    """A 400 carrying a machine-readable ``code`` the page localizes."""
    return web.json_response({"error": error, "code": code}, status=400)


def _reject_non_finite(value: str) -> Any:
    raise ValueError(f"non-finite number not allowed: {value}")


async def _json_object(request: web.Request) -> tuple[dict[str, Any] | None, web.Response | None]:
    """The body as a JSON object, or a 400."""
    try:
        body = await request.json(loads=lambda t: json.loads(t, parse_constant=_reject_non_finite))
    except (LookupError, RecursionError, ValueError):
        return None, _bad_request("invalid JSON", "invalid_json")
    if not isinstance(body, dict):
        return None, _bad_request("body must be a JSON object", "body_not_object")
    return body, None


def _text(
    body: dict[str, Any], key: str, limit: int, *, required: bool = False
) -> tuple[str, web.Response | None]:
    """A stripped string field of at most *limit* characters."""
    value = body.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        return "", _bad_request(f"{key} must be a string", "invalid_field_type")
    value = value.strip()
    if required and not value:
        return "", _bad_request(f"{key} is required", "missing_required_field")
    if len(value) > limit:
        return "", _bad_request(f"{key} is too long", "field_too_long")
    return value, None


def _tags(value: Any) -> list[str] | None:
    """A short list of short non-empty strings, or ``None`` when the shape is wrong."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_TAGS:
        return None
    if not all(isinstance(t, str) and 0 < len(t.strip()) <= _MAX_TAG for t in value):
        return None
    return [t.strip() for t in value]


def _pick(raw: Any) -> tuple[dict[str, Any] | None, str]:
    """A checked pick, or ``(None, code)``."""
    if not isinstance(raw, dict):
        return None, "invalid_pick"
    out: dict[str, Any] = {}
    for key, limit, required in (
        ("title", _MAX_TEXT, True),
        ("artist", _MAX_TEXT, False),
        ("why", _MAX_WHY, False),
    ):
        value, err = _text(raw, key, limit, required=required)
        if err is not None:
            return None, "invalid_pick"
        out[key] = value
    tags = _tags(raw.get("tags"))
    if tags is None:
        return None, "invalid_pick"
    out["tags"] = tags
    source = raw.get("source")
    if source == "youtube":
        video_id = raw.get("video_id")
        vid = (
            links.valid_video_id(video_id)
            if video_id is not None
            else links.youtube_video_id(raw.get("url"))
        )
        if vid is None:
            return None, "invalid_video_id"
        out.update(source="youtube", video_id=vid)
    elif source == "bilibili":
        url = links.bilibili_url(raw.get("url"))
        if url is None:
            return None, "invalid_bilibili_url"
        out.update(source="bilibili", url=url)
    else:
        return None, "invalid_source"
    return out, ""


def _require_enabled(handler):
    """Deny every request while Mood Music is disabled."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"error": "mood-music is disabled", "code": "app_disabled"}, status=403
            )
        return await handler(request)

    return _wrapped


def _owner_route(operation: str, handler):
    """Any route: the enable gate, then the owner gate for any dashboard caller."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        # A dashboard subject and a request with no app claim at all both go
        # through the owner gate; an app token was already confined by its grant.
        if not request.get("app"):
            denied = await require_owner_dashboard_request(request, f"mood_music.{operation}")
            if denied is not None:
                return denied
        return await handler(request)

    return _require_enabled(_wrapped)


async def _handle_state(request: web.Request) -> web.Response:
    state = await asyncio.to_thread(lambda: _store().snapshot())
    return web.json_response(state)


async def _handle_add_moment(request: web.Request) -> web.Response:
    body, err = await _json_object(request)
    if err is not None:
        return err
    assert body is not None
    mood, err = _text(body, "mood", _MAX_MOOD, required=True)
    if err is not None:
        return err
    note, err = _text(body, "note", _MAX_NOTE)
    if err is not None:
        return err
    tags = _tags(body.get("tags"))
    if tags is None:
        return _bad_request("tags must be a short list of short strings", "invalid_field_type")
    moment = await asyncio.to_thread(lambda: _store().add_moment(mood, note=note, tags=tags))
    return web.json_response(moment, status=201)


async def _handle_set_queue(request: web.Request) -> web.Response:
    body, err = await _json_object(request)
    if err is not None:
        return err
    assert body is not None
    source, err = _text(body, "source", _MAX_SOURCE, required=True)
    if err is not None:
        return err
    moment_id = body.get("moment_id") or ""
    if not isinstance(moment_id, str) or (moment_id and not _ID_RE.fullmatch(moment_id)):
        return _bad_request("moment_id is not an id", "invalid_field_type")
    mood_tags = _tags(body.get("mood_tags"))
    if mood_tags is None:
        return _bad_request("mood_tags must be a short list of short strings", "invalid_field_type")
    raw_picks = body.get("picks")
    if not isinstance(raw_picks, list) or not 0 < len(raw_picks) <= MAX_QUEUE:
        return _bad_request(f"picks must be a list of 1 to {MAX_QUEUE}", "invalid_field_type")
    picks: list[dict[str, Any]] = []
    for raw in raw_picks:
        pick, code = _pick(raw)
        if pick is None:
            return _bad_request("a pick was refused", code)
        picks.append(pick)
    queue = await asyncio.to_thread(
        lambda: _store().set_queue(picks, source=source, moment_id=moment_id, mood_tags=mood_tags)
    )
    return web.json_response({"queue": queue})


async def _handle_feedback(request: web.Request) -> web.Response:
    pick_id = request.match_info["id"]
    if not _ID_RE.fullmatch(pick_id):
        return _bad_request("not a pick id", "invalid_field_type")
    body, err = await _json_object(request)
    if err is not None:
        return err
    assert body is not None
    verdict = body.get("verdict")
    if verdict not in VERDICTS:
        return _bad_request("verdict must be liked, skipped or empty", "invalid_field_type")
    pick = await asyncio.to_thread(lambda: _store().set_feedback(pick_id, verdict))
    if pick is None:
        return web.json_response({"error": "no such pick", "code": "not_found"}, status=404)
    return web.json_response(pick)


def register_routes(app: web.Application) -> None:
    """Register Mood Music routes on the gateway's aiohttp Application."""
    # The read is owner-only too: moments are how the person felt.
    app.router.add_get(f"{_PREFIX}/state", _owner_route("read_state", _handle_state))
    app.router.add_post(f"{_PREFIX}/moments", _owner_route("add_moment", _handle_add_moment))
    app.router.add_post(f"{_PREFIX}/queue", _owner_route("set_queue", _handle_set_queue))
    app.router.add_put(
        f"{_PREFIX}/queue/{{id}}/feedback", _owner_route("feedback", _handle_feedback)
    )
