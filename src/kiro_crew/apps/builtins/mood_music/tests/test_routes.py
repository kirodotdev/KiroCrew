"""Mood Music routes: input checked at the boundary, owner-only, refused when disabled.

The link refusals here are the load-bearing ones: a pick's link reaches an
iframe (YouTube) or a new tab (bilibili), so a request carrying one bad pick is
refused whole and nothing is stored.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.mood_music.backend import routes
from kiro_crew.apps.builtins.mood_music.backend.store import MoodStore

BASE = "/api/apps/mood-music"
OWNER = "owner-1"
ID = "dQw4w9WgXcQ"
_NO_APP_CLAIM = object()


def _yt(**over) -> dict:
    return {
        "title": "Song",
        "artist": "A",
        "why": "fits",
        "source": "youtube",
        "url": f"https://www.youtube.com/watch?v={ID}",
        **over,
    }


def _bili(url: str) -> dict:
    return {"title": "Song", "source": "bilibili", "url": url}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    store = MoodStore(tmp_path / "state.json", now=lambda: 1000.0)
    enabled = {"on": True}
    monkeypatch.setattr(routes, "_store", lambda: store)
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: enabled["on"])
    return SimpleNamespace(store=store, enabled=enabled, path=tmp_path / "state.json")


async def _call(method: str, path: str, body=None, *, user: str = OWNER, app: object = ""):
    @web.middleware
    async def _identity(request, handler):
        request["user"] = user
        if app is not _NO_APP_CLAIM:
            request["app"] = app
        return await handler(request)

    server_app = web.Application(middlewares=[_identity])
    server_app["state"] = SimpleNamespace(owner_id=OWNER)
    routes.register_routes(server_app)
    async with TestClient(TestServer(server_app)) as client:
        resp = await client.request(method, BASE + path, json=body)
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_a_mood_then_a_queue_then_feedback(env) -> None:
    status, moment = await _call("POST", "/moments", {"mood": "  想念  ", "note": "rain"})
    assert status == 201 and moment["mood"] == "想念" and moment["note"] == "rain"
    status, body = await _call(
        "POST",
        "/queue",
        {
            "source": "r1",
            "moment_id": moment["id"],
            "mood_tags": ["温柔"],
            "picks": [
                _yt(),
                _yt(url=f"https://youtu.be/{ID}"),
                _bili("https://www.bilibili.com/video/BV1xx411c7mD"),
            ],
        },
    )
    assert status == 200
    queue = body["queue"]
    assert [p["source"] for p in queue] == ["youtube", "youtube", "bilibili"]
    # A YouTube pick keeps only its id; the URL the agent gave is not stored.
    assert queue[0]["video_id"] == ID and "url" not in queue[0]
    assert queue[2]["url"] == "https://www.bilibili.com/video/BV1xx411c7mD"
    status, pick = await _call("PUT", f"/queue/{queue[0]['id']}/feedback", {"verdict": "liked"})
    assert (status, pick["feedback"]) == (200, "liked")
    status, state = await _call("GET", "/state")
    assert status == 200
    assert state["feedback"][0]["verdict"] == "liked"
    assert state["moments"][0]["songs"][0]["title"] == "Song"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pick", "code"),
    [
        (_yt(url=f"https://www.youtube.com/watch?v={ID}Q"), "invalid_video_id"),  # 12 chars
        (_yt(url=None, video_id=ID + "Q"), "invalid_video_id"),
        (_yt(url=None, video_id="dQw4w9WgXc!"), "invalid_video_id"),
        (_yt(url=f"https://www.youtube-nocookie.com/embed/{ID}"), "invalid_video_id"),
        (_bili("https://bilibili.com.evil.com/video/BV1"), "invalid_bilibili_url"),
        (_bili("http://www.bilibili.com/video/BV1"), "invalid_bilibili_url"),
        (_bili("https://evil.com/video/BV1"), "invalid_bilibili_url"),
        (
            {"title": "x", "source": "spotify", "url": "https://open.spotify.com/x"},
            "invalid_source",
        ),
        ({"source": "youtube", "video_id": ID}, "invalid_pick"),  # no title
        (_yt(tags="calm"), "invalid_pick"),
        ("not an object", "invalid_pick"),
    ],
)
async def test_one_bad_pick_refuses_the_whole_queue(env, pick, code) -> None:
    status, body = await _call("POST", "/queue", {"source": "r1", "picks": [_yt(), pick]})
    assert (status, body["code"]) == (400, code)
    assert not env.path.exists()


@pytest.mark.asyncio
async def test_a_plain_youtube_watch_link_is_accepted(env) -> None:
    # Not the nocookie host: the agent hands a normal watch link, and only its id is kept.
    status, body = await _call(
        "POST",
        "/queue",
        {"source": "r1", "picks": [_yt(url=f"https://www.youtube.com/watch?v={ID}")]},
    )
    assert status == 200 and body["queue"][0]["video_id"] == ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"mood": ""},
        {"mood": "   "},
        {},
        {"mood": 3},
        {"mood": "x" * 201},
        {"mood": "calm", "tags": "calm"},
        {"mood": "calm", "tags": [""]},
    ],
)
async def test_an_empty_or_malformed_mood_is_refused(env, body) -> None:
    status, payload = await _call("POST", "/moments", body)
    assert status == 400 and payload["code"]
    assert not env.path.exists()


@pytest.mark.asyncio
async def test_an_unknown_mood_word_is_just_text(env) -> None:
    status, moment = await _call("POST", "/moments", {"mood": "something-no-chip-names"})
    assert status == 201 and moment["mood"] == "something-no-chip-names"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"picks": [_yt()]},  # no source
        {"source": "r1", "picks": []},
        {"source": "r1", "picks": [_yt()] * 13},
        {"source": "r1", "picks": [_yt()], "moment_id": "../x"},
    ],
)
async def test_a_malformed_queue_request_is_refused(env, body) -> None:
    status, _ = await _call("POST", "/queue", body)
    assert status == 400


@pytest.mark.asyncio
async def test_feedback_checks_the_id_and_the_verdict(env) -> None:
    assert (await _call("PUT", "/queue/not-an-id/feedback", {"verdict": "liked"}))[0] == 400
    (pick,) = env.store.set_queue(
        [{"title": "t", "artist": "", "why": "", "tags": [], "source": "youtube", "video_id": ID}],
        source="r",
    )
    assert (await _call("PUT", f"/queue/{pick['id']}/feedback", {"verdict": "loved"}))[0] == 400
    assert (await _call("PUT", f"/queue/{'0' * 32}/feedback", {"verdict": "liked"}))[0] == 404


ROUTES = [
    ("GET", "/state", None),
    ("POST", "/moments", {"mood": "calm"}),
    ("POST", "/queue", {"source": "r1", "picks": [_yt()]}),
    ("PUT", f"/queue/{'0' * 32}/feedback", {"verdict": "liked"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_a_non_owner_dashboard_caller_is_refused(env, method, path, body) -> None:
    status, payload = await _call(method, path, body, user="allowlisted-2")
    assert (status, payload.get("code")) == (403, "owner_only")
    assert not env.path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_a_request_with_no_app_claim_is_refused(env, method, path, body) -> None:
    status, payload = await _call(method, path, body, app=_NO_APP_CLAIM)
    assert (status, payload.get("code")) == (403, "owner_only")


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_every_route_refuses_while_disabled(env, method, path, body) -> None:
    env.enabled["on"] = False
    status, payload = await _call(method, path, body)
    assert (status, payload.get("code")) == (403, "app_disabled")
