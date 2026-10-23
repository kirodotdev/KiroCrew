"""Dashboard speech requests carry their identity and stop with their requesting client."""

from __future__ import annotations

import asyncio
import base64
import io
import wave
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_voice
from kiro_crew.testing.wait import await_until


@pytest.fixture
def voice_app(monkeypatch):
    app = web.Application()
    state = MagicMock()
    app["state"] = state
    chat_voice.register_voice_lifecycle(app)
    app.router.add_post("/api/voice/synthesize", chat_voice.api_voice_synthesize)
    app.router.add_post("/api/voice/cancel", chat_voice.api_voice_cancel)
    monkeypatch.setattr(
        chat_voice,
        "_vc",
        MagicMock(provider="system", default_rate="100%", system_voice=""),
    )
    return app, state


@pytest.mark.asyncio
async def test_request_identity_rides_every_frame(voice_app, monkeypatch, tmp_path):
    app, state = voice_app
    clip = tmp_path / "reply.wav"
    with wave.open(str(clip), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(22050)
        out.writeframes(b"\x01\x02\x03\x04")

    async def synth(*_args, **_kwargs):
        return str(clip)

    monkeypatch.setattr(chat_voice, "synthesize_speech", synth)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/voice/synthesize",
            json={
                "text": "你好",
                "slot": "s1",
                "request_id": "request-1",
            },
        )
        assert response.status == 200
        assert (await response.json())["request_id"] == "request-1"
    calls = state.broadcast_ws.call_args_list
    assert [call.args[0] for call in calls] == ["voice_chunk", "voice_complete"]
    assert all(call.args[1]["request_id"] == "request-1" for call in calls)
    replay = base64.b64decode(calls[-1].args[1]["audio"])
    with wave.open(io.BytesIO(replay)) as audio:
        assert audio.readframes(audio.getnframes()) == b"\x01\x02\x03\x04"
    assert not clip.exists()


@pytest.mark.asyncio
async def test_cancel_stops_synthesis_and_frees_capacity(voice_app, monkeypatch):
    app, state = voice_app
    started = asyncio.Event()
    closed = asyncio.Event()

    async def synth(*_args, **_kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(chat_voice, "synthesize_speech", synth)
    async with TestClient(TestServer(app)) as client:
        pending = asyncio.create_task(
            client.post(
                "/api/voice/synthesize",
                json={
                    "text": "hello",
                    "slot": "s1",
                    "request_id": "stop-me",
                },
            )
        )
        try:
            await await_until(started)
            response = await client.post(
                "/api/voice/cancel", json={"slot": "s1", "request_id": "stop-me"}
            )
            assert response.status == 200
            await await_until(closed)
            assert not app[chat_voice._VOICE_REQUESTS].tasks
            state.broadcast_ws.assert_not_called()
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_before_synthesize_blocks_the_racing_request(voice_app, monkeypatch):
    app, state = voice_app
    synth = MagicMock()
    monkeypatch.setattr(chat_voice, "synthesize_speech", synth)
    async with TestClient(TestServer(app)) as client:
        identity = {"slot": "s1", "request_id": "already-stopped"}
        assert (await client.post("/api/voice/cancel", json=identity)).status == 200
        response = await client.post("/api/voice/synthesize", json={**identity, "text": "hello"})
        assert response.status == 409
        assert (await response.json())["code"] == "voice_cancelled"
    synth.assert_not_called()
    state.broadcast_ws.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        [],
        {"text": 12},
        {"text": "hello", "slot": []},
        {"text": "hello", "request_id": []},
        {"text": "hello", "rate": []},
    ],
)
async def test_malformed_requests_are_rejected_before_synthesis(voice_app, monkeypatch, body):
    app, _state = voice_app
    synth = MagicMock()
    monkeypatch.setattr(chat_voice, "synthesize_speech", synth)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/voice/synthesize", json=body)
        assert response.status == 400
        assert (await response.json())["code"] == (
            "body_not_object" if isinstance(body, list) else "voice_invalid_request"
        )
    synth.assert_not_called()
