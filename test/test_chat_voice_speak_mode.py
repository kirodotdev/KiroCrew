"""``voice_reply.speak_mode`` through the dashboard: the config endpoint's new field
and ``POST /api/voice/summarize``, the text Auto-speak and Read aloud speak.

The summary step itself is covered in ``test_voice_summary.py``; here it is stubbed,
and the tests lock in the endpoint's contract: a bad request is refused before any
model call, ``full`` mode echoes the text, a declined summary echoes the text, and
``summary`` mode without background sessions is a 503, never a silent reply.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state


def _make_voice_app(state) -> web.Application:
    from kiro_crew.dashboard.chat_voice import api_voice_config, api_voice_summarize

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/voice/config", api_voice_config)
    app.router.add_put("/api/voice/config", api_voice_config)
    app.router.add_post("/api/voice/summarize", api_voice_summarize)
    return app


def _voice_config_mock(**over) -> MagicMock:
    """A ``_vc`` stand-in with every field the config GET reads, overridable."""
    fields = dict(
        global_enabled=True,
        auto_speak=True,
        speak_mode="full",
        provider="system",
        default_voice="Ruth",
        default_engine="generative",
        default_rate="100%",
        default_pitch="0%",
        aws_profile="",
        region="",
        system_voice="",
        language_code="",
    )
    fields.update(over)
    return MagicMock(**fields)


@pytest.fixture
def voice_app(tmp_path, monkeypatch):
    """A voice app over a mocked ``_vc`` and a writable config.json.

    Returns ``(client_factory, mock_vc, cfg_path, state)``; ``client_factory(mode)``
    sets ``speak_mode`` first so a test names the mode it is about.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    mock_vc = _voice_config_mock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"voice_reply": {"auto_speak": True}}))
    monkeypatch.setattr("kiro_crew.dashboard.chat_voice.config_path", lambda: cfg_path)
    state = _make_state(tmp_path)

    def client(mode: str = "full") -> TestClient:
        mock_vc.speak_mode = mode
        return TestClient(TestServer(_make_voice_app(state)))

    return client, mock_vc, cfg_path, state


class TestSpeakMode:
    """``voice_reply.speak_mode`` through the dashboard's config endpoint."""

    @pytest.mark.asyncio
    async def test_get_and_put_round_trip_speak_mode(self, voice_app):
        client, mock_vc, cfg_path, _ = voice_app
        async with client("summary") as c:
            assert (await (await c.get("/api/voice/config")).json())["speakMode"] == "summary"
            resp = await c.put("/api/voice/config", json={"speakMode": "full"})
            assert resp.status == 200
            assert mock_vc.speak_mode == "full"
            # The other flags are untouched: the mode is its own setting.
            assert mock_vc.auto_speak is True
            assert (await (await c.get("/api/voice/config")).json())["speakMode"] == "full"
        persisted = json.loads(cfg_path.read_text(encoding="utf-8"))["voice_reply"]
        assert persisted == {"auto_speak": True, "speak_mode": "full"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "value", ["brief", "", 1, ["summary"]], ids=["word", "empty", "int", "list"]
    )
    async def test_put_config_refuses_an_unknown_speak_mode(self, voice_app, value):
        client, mock_vc, cfg_path, _ = voice_app
        async with client() as c:
            resp = await c.put("/api/voice/config", json={"speakMode": value, "autoSpeak": False})
            assert resp.status == 400
            assert (await resp.json())["code"] == "voice_speak_mode_invalid"
        # A refused patch applies none of its fields, the mode included.
        assert (mock_vc.speak_mode, mock_vc.auto_speak) == ("full", True)
        assert "speak_mode" not in json.loads(cfg_path.read_text(encoding="utf-8"))["voice_reply"]


class TestVoiceSummarize:
    """``POST /api/voice/summarize``: what the dashboard speaks for a finished reply."""

    _REPLY = "I ran every test and they passed. " * 12

    @pytest.fixture
    def summarizer(self, monkeypatch):
        """Record what the summary step is handed and script its answer."""
        calls: list[dict] = []
        answer: dict = {"value": "A short spoken summary."}

        async def _fake(sessions, text, *, spoken):
            calls.append({"sessions": sessions, "text": text, "spoken": spoken})
            return answer["value"]

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.summarize_for_speech", _fake)
        return calls, answer

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "status", "code"),
        [
            ({}, 400, "voice_invalid_request"),
            ({"text": ""}, 400, "voice_invalid_request"),
            ({"text": 7}, 400, "voice_invalid_request"),
            ({"text": "fine", "request_id": 3}, 400, "voice_invalid_request"),
            ({"text": "fine", "request_id": "x" * 129}, 400, "voice_invalid_request"),
            ({"text": "x" * 600_000}, 413, "payload_too_large"),
        ],
        ids=["no-text", "empty", "non-string", "id-not-string", "id-too-long", "oversized-body"],
    )
    async def test_a_bad_request_is_refused_before_any_model_call(
        self, voice_app, summarizer, body, status, code
    ):
        client, _, _, _ = voice_app
        calls, _ = summarizer
        async with client("summary") as c:
            resp = await c.post("/api/voice/summarize", json=body)
            assert (resp.status, (await resp.json())["code"]) == (status, code)
        assert calls == []

    @pytest.mark.asyncio
    async def test_full_mode_echoes_the_text_without_a_model_call(self, voice_app, summarizer):
        client, _, _, _ = voice_app
        calls, _ = summarizer
        async with client("full") as c:
            resp = await c.post(
                "/api/voice/summarize", json={"text": self._REPLY, "request_id": "r1"}
            )
            assert resp.status == 200
            data = await resp.json()
        assert data == {
            "ok": True,
            "text": self._REPLY.strip(),
            "summarized": False,
            "request_id": "r1",
        }
        assert calls == []

    @pytest.mark.asyncio
    async def test_summary_mode_without_sessions_is_503(self, voice_app, summarizer):
        client, _, _, state = voice_app
        calls, _ = summarizer
        state.sessions = None
        async with client("summary") as c:
            resp = await c.post("/api/voice/summarize", json={"text": self._REPLY})
            assert resp.status == 503
            assert (await resp.json())["code"] == "voice_summary_unavailable"
        assert calls == []

    @pytest.mark.asyncio
    async def test_summary_mode_speaks_the_summary(self, voice_app, summarizer):
        client, _, _, state = voice_app
        calls, _ = summarizer
        async with client("summary") as c:
            resp = await c.post(
                "/api/voice/summarize",
                json={"text": "```\nmake test\n```\n" + self._REPLY, "request_id": "r2"},
            )
            assert resp.status == 200
            data = await resp.json()
        assert data == {
            "ok": True,
            "text": "A short spoken summary.",
            "summarized": True,
            "request_id": "r2",
        }
        assert len(calls) == 1 and calls[0]["sessions"] is state.sessions
        # The step gets the reply as written and its stripped, speakable form.
        assert calls[0]["text"].startswith("```")
        assert "```" not in calls[0]["spoken"]

    @pytest.mark.asyncio
    async def test_a_long_turn_is_still_summarized(self, voice_app, summarizer):
        client, _, _, _ = voice_app
        calls, _ = summarizer
        # Past the shared 64 KiB body cap, under this route's own: the whole turn
        # must reach the gateway so it is stripped before it is clipped.
        long = "I ran every test and they passed. " * 2500  # about 85,000 characters
        async with client("summary") as c:
            resp = await c.post("/api/voice/summarize", json={"text": long})
            assert resp.status == 200
            assert (await resp.json())["summarized"] is True
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_declined_summary_speaks_the_full_reply(self, voice_app, summarizer):
        client, _, _, _ = voice_app
        calls, answer = summarizer
        answer["value"] = None
        async with client("summary") as c:
            resp = await c.post("/api/voice/summarize", json={"text": self._REPLY})
            assert resp.status == 200
            data = await resp.json()
        assert (data["summarized"], data["text"]) == (False, self._REPLY.strip())
        assert len(calls) == 1
