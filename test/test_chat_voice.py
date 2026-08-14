"""Unit tests for chat_voice.py — voice config and synthesis endpoints."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state


def _make_voice_app(state):
    from kiro_crew.dashboard.chat_voice import api_voice_config, api_voice_synthesize

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/voice/config", api_voice_config)
    app.router.add_put("/api/voice/config", api_voice_config)
    app.router.add_post("/api/voice/synthesize", api_voice_synthesize)
    return app


class TestVoiceConfig:
    @pytest.mark.asyncio
    async def test_get_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=True, provider="polly", default_voice="Joanna",
            default_engine="neural", default_rate="100%", default_pitch="0%",
            aws_profile="", region="us-east-1", piper_binary="", piper_model="",
            piper_model_config="", piper_length_scale=1.0,
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.get("/api/voice/config")
            assert resp.status == 200
            data = await resp.json()
            assert data["voice"] == "Joanna"
            assert data["engine"] == "neural"
            assert data["enabled"] is True

    @pytest.mark.asyncio
    async def test_put_config_updates_voice(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=False, default_voice="Joanna", default_engine="neural",
            default_rate="100%", default_pitch="0%", aws_profile="", region="us-east-1",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        # Write a config file so PUT can persist
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({}))
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.config_path", lambda: cfg_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put("/api/voice/config", json={"voice": "Matthew", "enabled": True})
            assert resp.status == 200
            assert mock_vc.default_voice == "Matthew"
            assert mock_vc.global_enabled is True

    @pytest.mark.asyncio
    async def test_get_config_exposes_provider_and_piper(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=True, provider="piper", default_voice="Ruth",
            default_engine="generative", default_rate="100%", default_pitch="0%",
            aws_profile="", region="", piper_binary="/usr/bin/piper",
            piper_model="~/m.onnx", piper_model_config="", piper_length_scale=1.0,
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.get("/api/voice/config")
            assert resp.status == 200
            data = await resp.json()
            assert data["provider"] == "piper"
            assert data["piper_binary"] == "/usr/bin/piper"
            assert data["piper_model"] == "~/m.onnx"
            assert data["piper_length_scale"] == 1.0

    @pytest.mark.asyncio
    async def test_put_config_updates_provider_and_piper(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=False, provider="polly", default_voice="Joanna",
            default_engine="neural", default_rate="100%", default_pitch="0%",
            aws_profile="", region="", piper_binary="", piper_model="",
            piper_model_config="", piper_length_scale=1.0,
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({}))
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.config_path", lambda: cfg_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put("/api/voice/config", json={
                "provider": "piper", "piper_model": " ~/voices/en.onnx ",
                "piper_length_scale": 1.5,
            })
            assert resp.status == 200
            assert mock_vc.provider == "piper"
            assert mock_vc.piper_model == "~/voices/en.onnx"  # stripped
            assert mock_vc.piper_length_scale == 1.5
        # Persisted to config.json under voice_reply
        persisted = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert persisted["voice_reply"]["provider"] == "piper"
        assert persisted["voice_reply"]["piper_model"] == "~/voices/en.onnx"

    @pytest.mark.asyncio
    async def test_put_config_rejects_invalid_provider(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=False, provider="piper", default_voice="Ruth",
            default_engine="generative", default_rate="100%", default_pitch="0%",
            aws_profile="", region="", piper_binary="", piper_model="",
            piper_model_config="", piper_length_scale=1.0,
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({}))
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.config_path", lambda: cfg_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put("/api/voice/config", json={"provider": "bogus"})
            assert resp.status == 200
            # Invalid provider ignored — unchanged
            assert mock_vc.provider == "piper"

    @pytest.mark.asyncio
    async def test_put_config_unhashable_engine_does_not_500(self, tmp_path, monkeypatch):
        # `body["engine"] in VALID_ENGINES` (a frozenset) raises
        # TypeError: unhashable type on a JSON list/dict value, 500ing the PUT.
        # The provider check above was already guarded; engine was missed.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=False, provider="piper", default_voice="Ruth",
            default_engine="generative", default_rate="100%", default_pitch="0%",
            aws_profile="", region="", piper_binary="", piper_model="",
            piper_model_config="", piper_length_scale=1.0,
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({}))
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.config_path", lambda: cfg_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            for bad in ({"engine": ["neural"]}, {"engine": {"x": 1}}):
                resp = await client.put("/api/voice/config", json=bad)
                assert resp.status == 200  # not a 500
            # Unhashable/non-str engine ignored — unchanged
            assert mock_vc.default_engine == "generative"

    @pytest.mark.asyncio
    async def test_put_config_ignores_invalid_length_scale(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=False, provider="piper", default_voice="Ruth",
            default_engine="generative", default_rate="100%", default_pitch="0%",
            aws_profile="", region="", piper_binary="", piper_model="",
            piper_model_config="", piper_length_scale=1.0,
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({}))
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.config_path", lambda: cfg_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            # Non-numeric, huge-int (OverflowError), non-finite, and non-positive
            # values must all be rejected WITHOUT a 500 and WITHOUT persisting an
            # unserializable value — each leaves the field unchanged at 1.0.
            for bad in ["fast", 10 ** 400, float("inf"), float("nan"), 0, -2.0]:
                resp = await client.put(
                    "/api/voice/config", json={"piper_length_scale": bad}
                )
                assert resp.status == 200, f"{bad!r} should not 500"
                assert mock_vc.piper_length_scale == 1.0, f"{bad!r} should be ignored"

    @pytest.mark.asyncio
    async def test_put_config_unhashable_provider_does_not_500(self, tmp_path, monkeypatch):
        # `body["provider"] in VALID_PROVIDERS` would raise TypeError on an
        # unhashable JSON value (list/dict); the isinstance(str) guard prevents it.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=False, provider="piper", default_voice="Ruth",
            default_engine="generative", default_rate="100%", default_pitch="0%",
            aws_profile="", region="", piper_binary="", piper_model="",
            piper_model_config="", piper_length_scale=1.0,
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({}))
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.config_path", lambda: cfg_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put("/api/voice/config", json={"provider": ["piper"]})
            assert resp.status == 200
            assert mock_vc.provider == "piper"  # unchanged, not crashed

    @pytest.mark.asyncio
    async def test_put_config_preserves_unmanaged_voice_reply_keys(self, tmp_path, monkeypatch):
        # The PUT persists a fixed key set but the loader also reads auto_speak /
        # auto_reply_to_voice from voice_reply — a wholesale rewrite would drop
        # them. Merge must preserve keys this handler doesn't manage.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=True, provider="polly", default_voice="Joanna",
            default_engine="neural", default_rate="100%", default_pitch="0%",
            aws_profile="", region="", piper_binary="", piper_model="",
            piper_model_config="", piper_length_scale=1.0,
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "voice_reply": {"enabled": True, "auto_reply_to_voice": False, "auto_speak": True}
        }))
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.config_path", lambda: cfg_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put("/api/voice/config", json={"voice": "Matthew"})
            assert resp.status == 200
        persisted = json.loads(cfg_path.read_text(encoding="utf-8"))["voice_reply"]
        assert persisted["voice_id"] == "Matthew"       # updated
        assert persisted["auto_reply_to_voice"] is False  # preserved (not dropped)
        assert persisted["auto_speak"] is True            # preserved

    @pytest.mark.asyncio
    async def test_synthesize_routes_piper_through_nonstreaming(self, tmp_path, monkeypatch):
        # With provider=piper the dashboard synth must NOT call the Polly-only
        # streaming path; it routes through synthesize_speech and emits one chunk.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            provider="piper", default_voice="Ruth", default_engine="generative",
            default_rate="100%", default_pitch="0%", aws_profile="", region="",
            piper_binary="", piper_model="~/m.onnx", piper_model_config="",
            piper_length_scale=1.0,
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)

        wav = tmp_path / "out.wav"
        wav.write_bytes(b"RIFF....WAVEfake-audio-bytes")

        async def _fake_synth(text, **kw):
            assert kw["provider"] == "piper"
            return str(wav)

        streaming_called = False

        async def _fake_stream(*a, **kw):
            nonlocal streaming_called
            streaming_called = True
            if False:
                yield  # pragma: no cover — make it an async generator

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.synthesize_speech", _fake_synth)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.streaming_voice_reply", _fake_stream)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "hello", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True and data["chunks"] == 1
        assert streaming_called is False  # Polly path NOT used for Piper
        kinds = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "voice_chunk" in kinds and "voice_complete" in kinds

    @pytest.mark.asyncio
    async def test_put_config_invalid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put("/api/voice/config", data=b"not json", headers={"Content-Type": "application/json"})
            assert resp.status == 400


class TestVoiceSynthesize:
    @pytest.mark.asyncio
    async def test_synthesize_empty_text_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "", "slot": "s1"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_synthesize_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            default_voice="Joanna", default_engine="neural",
            default_rate="100%", default_pitch="0%", aws_profile="", region="us-east-1",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)

        # Mock streaming_voice_reply to yield one chunk
        async def mock_stream(*a, **kw):
            yield 0, "Hello", b"\x00\x01\x02"

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.streaming_voice_reply", mock_stream)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.stitch_mp3s", AsyncMock(return_value=None))

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "Hello world", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["chunks"] == 1
        state.broadcast_ws.assert_called()

    @pytest.mark.asyncio
    async def test_synthesize_exception_returns_500_and_broadcasts_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            default_voice="Joanna", default_engine="neural",
            default_rate="100%", default_pitch="0%", aws_profile="", region="us-east-1",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)

        # Mock streaming_voice_reply to raise an exception
        async def mock_stream_error(*a, **kw):
            raise RuntimeError("Polly synthesis failed")
            yield  # noqa: unreachable - makes this a generator

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.streaming_voice_reply", mock_stream_error)

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "Hello", "slot": "s1"})
            assert resp.status == 500
            data = await resp.json()
            assert data["ok"] is False
            assert "error" in data
        # Verify voice_error was broadcast
        state.broadcast_ws.assert_called()
        call_args = state.broadcast_ws.call_args
        assert call_args[0][0] == "voice_error"
        assert call_args[0][1]["slot"] == "s1"


class TestVoiceVoices:
    @pytest.mark.asyncio
    async def test_voices_returns_list(self, tmp_path, monkeypatch):
        """Test successful voice listing."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(aws_profile="polly", region="us-east-1")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        # Reset cache
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache", None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache_ts", 0)

        mock_data = json.dumps({"Voices": [
            {"Id": "Takumi", "Name": "Takumi", "LanguageName": "Japanese",
             "LanguageCode": "ja-JP", "Gender": "Male", "SupportedEngines": ["neural", "standard"]},
            {"Id": "Mizuki", "Name": "Mizuki", "LanguageName": "Japanese",
             "LanguageCode": "ja-JP", "Gender": "Female", "SupportedEngines": ["standard"]},
        ]})

        async def mock_exec(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 0

            async def comm():
                return mock_data.encode(), b""
            proc.communicate = comm
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_exec)
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/aws")

        from kiro_crew.dashboard.chat_voice import api_voice_voices
        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/voice/voices", api_voice_voices)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/voice/voices")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["voices"]) == 2
            assert data["voices"][0]["id"] == "Mizuki"  # sorted by languageCode+name
            assert "engines" in data["voices"][0]

    @pytest.mark.asyncio
    async def test_voices_uses_cache(self, tmp_path, monkeypatch):
        """Test that cached voices are returned without subprocess call."""
        import time
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(aws_profile="", region="")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        cached = [
            {"id": "Ruth", "name": "Ruth", "language": "English",
             "languageCode": "en-US", "gender": "Female", "engines": ["neural"]}
        ]
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache", cached)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache_ts", time.time())

        from kiro_crew.dashboard.chat_voice import api_voice_voices
        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/voice/voices", api_voice_voices)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/voice/voices")
            assert resp.status == 200
            data = await resp.json()
            assert data["voices"] == cached

    @pytest.mark.asyncio
    async def test_voices_cli_failure(self, tmp_path, monkeypatch):
        """Test error handling when aws cli fails."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(aws_profile="", region="")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache", None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache_ts", 0)

        async def mock_exec(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 1

            async def comm():
                return b"", b"AccessDenied"
            proc.communicate = comm
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_exec)
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/aws")

        from kiro_crew.dashboard.chat_voice import api_voice_voices
        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/voice/voices", api_voice_voices)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/voice/voices")
            assert resp.status == 502

    @pytest.mark.asyncio
    async def test_voices_timeout(self, tmp_path, monkeypatch):
        """Test timeout handling."""
        import asyncio
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(aws_profile="", region="")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache", None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache_ts", 0)

        async def mock_exec(*args, **kwargs):
            proc = MagicMock()

            async def comm():
                raise asyncio.TimeoutError()
            proc.communicate = comm
            proc.kill = MagicMock()

            async def _wait():
                return 0
            proc.wait = _wait
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_exec)
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/aws")

        from kiro_crew.dashboard.chat_voice import api_voice_voices
        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/voice/voices", api_voice_voices)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/voice/voices")
            assert resp.status == 504

    @pytest.mark.asyncio
    async def test_voices_aws_not_found(self, tmp_path, monkeypatch):
        """aws CLI absent from PATH → 200 with empty list, no subprocess spawn."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(aws_profile="", region="")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache", None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache_ts", 0)

        monkeypatch.setattr("shutil.which", lambda cmd: None)
        spawn = AsyncMock()
        monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)

        from kiro_crew.dashboard.chat_voice import api_voice_voices
        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/voice/voices", api_voice_voices)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/voice/voices")
            assert resp.status == 200
            data = await resp.json()
            assert data == {"voices": []}
        spawn.assert_not_called()
        # The empty result must NOT be cached — the list should recover
        # as soon as `aws` becomes resolvable.
        from kiro_crew.dashboard import chat_voice
        assert chat_voice._voices_cache is None

    @pytest.mark.asyncio
    async def test_voices_exec_file_not_found(self, tmp_path, monkeypatch):
        """which() succeeds but the exec itself raises FileNotFoundError
        (binary removed in between, or a script with a missing interpreter)
        → same graceful empty-list degrade, no 500."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(aws_profile="", region="")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache", None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache_ts", 0)

        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/aws")

        async def mock_exec(*args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "aws")

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_exec)

        from kiro_crew.dashboard.chat_voice import api_voice_voices
        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/voice/voices", api_voice_voices)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/voice/voices")
            assert resp.status == 200
            data = await resp.json()
            assert data == {"voices": []}
