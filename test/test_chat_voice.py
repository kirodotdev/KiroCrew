"""Unit tests for chat_voice.py — voice config and synthesis endpoints."""

from __future__ import annotations

import asyncio
import builtins
import json
import textwrap
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state


def _make_voice_app(state):
    from kiro_crew.dashboard.chat_voice import (
        api_voice_cancel,
        api_voice_config,
        api_voice_synthesize,
        register_voice_lifecycle,
    )

    app = web.Application()
    app["state"] = state
    register_voice_lifecycle(app)
    app.router.add_get("/api/voice/config", api_voice_config)
    app.router.add_put("/api/voice/config", api_voice_config)
    app.router.add_post("/api/voice/synthesize", api_voice_synthesize)
    app.router.add_post("/api/voice/cancel", api_voice_cancel)
    return app


class TestVoiceConfig:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "expect_code"),
        [
            (["system_voice"], True),
            (["engine"], True),
            ("system_voice", True),
            (42, True),
            (None, False),
        ],
        ids=["list-with-key", "list-with-legacy-key", "bare-string", "number", "null"],
    )
    async def test_non_object_body_is_rejected_not_a_500(
        self, tmp_path, monkeypatch, body, expect_code
    ):
        """A JSON body that is not an object must be a 400, never a crash.

        ``"system_voice" in body`` is a MEMBERSHIP test over a list's elements, so
        ``["system_voice"]`` passes that guard and then raises TypeError on the
        subscript — an unhandled 500. A bare string or number fails the same way,
        and the synthesize endpoint's ``body.get`` raises AttributeError. Covers a
        key this PR adds and a pre-existing one, because the shape check has to be
        per-request, not per-key.

        The guard is `read_bounded_json`, the shared helper that already owns this
        for the endpoints routed through it, so the code asserted is its
        ``body_not_object`` rather than a fifth private spelling — the divergence
        tracked on issue #5587.

        ``None`` reaches the 400 through the parse branch instead, which reports
        ``invalid_json``; asserted separately rather than folded in, so this test
        cannot be read as claiming a guarantee that branch does not make.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put("/api/voice/config", json=body)
            assert resp.status == 400, f"{body!r} produced {resp.status}"
            if expect_code:
                assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.parametrize(
        "field",
        [
            "voice",
            "system_voice",
            "piper_binary",
            "piper_model",
            "piper_model_config",
            "aws_profile",
            "region",
        ],
    )
    @pytest.mark.parametrize("bad", [{}, [], 3, True])
    @pytest.mark.asyncio
    async def test_non_string_config_field_is_rejected_not_stringified(
        self, tmp_path, monkeypatch, field, bad
    ):
        """A wrong-typed string field must 400, never persist ``str(value)``.

        ``str({})`` is ``"{}"`` — a syntactically fine voice name or binary path
        that no engine can ever satisfy, so synthesis returns silence while the
        stored config looks populated. That is the failure this rejects: silent,
        persistent, and indistinguishable from a broken engine.

        Rejecting rather than coercing to ``""`` matters because ``""`` is itself
        meaningful for these fields (an empty ``system_voice`` selects the OS
        default voice), so coercion would rewrite "set this voice" into "use the
        default" without telling the caller.

        Every name/path field the handler writes is covered, not just the one this
        feature adds. Covering a subset is what made this the same finding four
        rounds running: each round fixed the reported field and left its siblings,
        so the reviewer simply named the next one. ``rate``/``pitch`` are absent on
        purpose -- their validators own a documented degrade-to-default contract, so
        they coerce rather than reject. ``True`` is included
        because ``bool`` is the case a naive ``isinstance(v, (str, int))`` guard
        lets through.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put("/api/voice/config", json={field: bad})
            assert resp.status == 400, f"{field}={bad!r} produced {resp.status}"
            payload = await resp.json()
            assert payload["code"] == "field_not_string"
            assert field in payload["error"]
        # Nothing was persisted: a rejected write must not leave the garbage behind.
        cfg = tmp_path / "config.json"
        if cfg.exists():
            assert "{}" not in cfg.read_text()

    @pytest.mark.parametrize("field", ["enabled", "autoSpeak"])
    @pytest.mark.parametrize("bad", ["false", "true", 0, 1, {}, []])
    @pytest.mark.asyncio
    async def test_non_boolean_flag_is_rejected_not_coerced(
        self, tmp_path, monkeypatch, field, bad
    ):
        """A wrong-typed flag must 400 rather than pass through ``bool()``.

        ``bool()`` never raises, so it looks like a safe total function, but it is
        not truth-preserving over what clients send: ``bool("false")`` is ``True``.
        A caller passing the string ``"false"`` to disable voice would have ENABLED
        it, which is the worst shape of this bug -- the stored setting is the exact
        opposite of the request and nothing reports it.

        ``0``/``1`` are included because they are the other plausible spelling a
        client reaches for, and they are equally not booleans.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put("/api/voice/config", json={field: bad})
            assert resp.status == 400, f"{field}={bad!r} produced {resp.status}"
            assert (await resp.json())["code"] == "field_not_boolean"

    @pytest.mark.asyncio
    async def test_a_rejected_patch_applies_none_of_its_fields(self, tmp_path, monkeypatch):
        """A 400 must leave `_vc` untouched, including fields validated earlier.

        `PUT {"provider": "piper", "system_voice": {}}` names a VALID provider and
        an invalid voice. Applying fields while walking them makes this a torn
        write: the caller is told 400 while the live provider has already changed,
        so the next synthesis silently uses an engine the request never
        established. The handler validates the whole patch first and applies it in
        one pass, so a rejected request is a no-op.

        `provider` is the field worth pinning because it is ordered FIRST and
        routes synthesis -- the widest blast radius of anything here.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from kiro_crew.dashboard import chat_voice as cv

        before = cv._vc.provider
        other = next(p for p in ("piper", "polly", "system") if p != before)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put(
                "/api/voice/config", json={"provider": other, "system_voice": {}}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "field_not_string"
        assert cv._vc.provider == before, "a refused PUT changed the live provider"

    @pytest.mark.asyncio
    async def test_get_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=True,
            auto_speak=False,
            provider="polly",
            default_voice="Joanna",
            default_engine="neural",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="us-east-1",
            piper_binary="",
            piper_model="",
            piper_model_config="",
            piper_length_scale=1.0,
            system_voice="",
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
            # autoSpeak reflects the dedicated auto_speak field, not `enabled` —
            # they're independent toggles in the Settings UI.
            assert data["autoSpeak"] is False

    @pytest.mark.asyncio
    async def test_get_config_auto_speak_independent_of_enabled(self, tmp_path, monkeypatch):
        # Regression test: `autoSpeak` used to alias `global_enabled`, so a user
        # with voice enabled but auto-speak off would still get auto-spoken
        # replies (and vice versa). The two must be reported independently.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=True,
            auto_speak=False,
            provider="piper",
            default_voice="Ruth",
            default_engine="generative",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="",
            piper_binary="",
            piper_model="",
            piper_model_config="",
            piper_length_scale=1.0,
            system_voice="",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.get("/api/voice/config")
            data = await resp.json()
            assert data["enabled"] is True
            assert data["autoSpeak"] is False

    @pytest.mark.asyncio
    async def test_put_config_updates_voice(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=False,
            auto_speak=False,
            default_voice="Joanna",
            default_engine="neural",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="us-east-1",
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
    async def test_put_config_updates_auto_speak_independently_of_enabled(
        self, tmp_path, monkeypatch
    ):
        # Regression test: PUT {"autoSpeak": ...} used to flip `global_enabled`
        # (the primary voice switch) instead of the dedicated `auto_speak` field —
        # so unchecking "Auto-speak responses" in Settings silently disabled
        # voice entirely, including the manual speak button.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=True,
            auto_speak=True,
            provider="piper",
            default_voice="Ruth",
            default_engine="generative",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="",
            piper_binary="",
            piper_model="",
            piper_model_config="",
            piper_length_scale=1.0,
            system_voice="",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({}))
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.config_path", lambda: cfg_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put("/api/voice/config", json={"autoSpeak": False})
            assert resp.status == 200
            assert mock_vc.auto_speak is False
            # Turning auto-speak off must NOT also disable voice globally.
            assert mock_vc.global_enabled is True
        persisted = json.loads(cfg_path.read_text(encoding="utf-8"))["voice_reply"]
        assert persisted["auto_speak"] is False

    @pytest.mark.asyncio
    async def test_get_config_exposes_provider_and_piper(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=True,
            auto_speak=False,
            provider="piper",
            default_voice="Ruth",
            default_engine="generative",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="",
            piper_binary="/usr/bin/piper",
            piper_model="~/m.onnx",
            piper_model_config="",
            piper_length_scale=1.0,
            system_voice="",
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
            global_enabled=False,
            auto_speak=False,
            provider="polly",
            default_voice="Joanna",
            default_engine="neural",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="",
            piper_binary="",
            piper_model="",
            piper_model_config="",
            piper_length_scale=1.0,
            system_voice="",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({}))
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.config_path", lambda: cfg_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put(
                "/api/voice/config",
                json={
                    "provider": "piper",
                    "piper_model": " ~/voices/en.onnx ",
                    "piper_length_scale": 1.5,
                },
            )
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
            global_enabled=False,
            auto_speak=False,
            provider="piper",
            default_voice="Ruth",
            default_engine="generative",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="",
            piper_binary="",
            piper_model="",
            piper_model_config="",
            piper_length_scale=1.0,
            system_voice="",
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
            global_enabled=False,
            auto_speak=False,
            provider="piper",
            default_voice="Ruth",
            default_engine="generative",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="",
            piper_binary="",
            piper_model="",
            piper_model_config="",
            piper_length_scale=1.0,
            system_voice="",
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
            global_enabled=False,
            auto_speak=False,
            provider="piper",
            default_voice="Ruth",
            default_engine="generative",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="",
            piper_binary="",
            piper_model="",
            piper_model_config="",
            piper_length_scale=1.0,
            system_voice="",
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
            for bad in ["fast", 10**400, float("inf"), float("nan"), 0, -2.0]:
                resp = await client.put("/api/voice/config", json={"piper_length_scale": bad})
                assert resp.status == 200, f"{bad!r} should not 500"
                assert mock_vc.piper_length_scale == 1.0, f"{bad!r} should be ignored"

    @pytest.mark.asyncio
    async def test_put_config_unhashable_provider_does_not_500(self, tmp_path, monkeypatch):
        # `body["provider"] in VALID_PROVIDERS` would raise TypeError on an
        # unhashable JSON value (list/dict); the isinstance(str) guard prevents it.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=False,
            auto_speak=False,
            provider="piper",
            default_voice="Ruth",
            default_engine="generative",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="",
            piper_binary="",
            piper_model="",
            piper_model_config="",
            piper_length_scale=1.0,
            system_voice="",
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
        # The PUT persists a fixed key set but the loader also reads
        # auto_reply_to_voice from voice_reply — a wholesale rewrite would drop
        # it. Merge must preserve keys this handler doesn't manage.
        # (auto_speak IS managed by this handler — see
        # test_put_config_updates_auto_speak_independently_of_enabled — so it's
        # written from the live `_vc.auto_speak`, not merely carried over.)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=True,
            auto_speak=True,
            provider="polly",
            default_voice="Joanna",
            default_engine="neural",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="",
            piper_binary="",
            piper_model="",
            piper_model_config="",
            piper_length_scale=1.0,
            system_voice="",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps(
                {"voice_reply": {"enabled": True, "auto_reply_to_voice": False, "auto_speak": True}}
            )
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.config_path", lambda: cfg_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put("/api/voice/config", json={"voice": "Matthew"})
            assert resp.status == 200
        persisted = json.loads(cfg_path.read_text(encoding="utf-8"))["voice_reply"]
        assert persisted["voice_id"] == "Matthew"  # updated
        assert persisted["auto_reply_to_voice"] is False  # preserved (not dropped)
        assert persisted["auto_speak"] is True  # written from _vc.auto_speak

    @pytest.mark.asyncio
    async def test_synthesize_routes_piper_through_pcm_stream(self, tmp_path, monkeypatch):
        # Provider dispatch keeps local audio independent of the Polly client.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            provider="piper",
            default_voice="Ruth",
            default_engine="generative",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="",
            piper_binary="",
            piper_model="~/m.onnx",
            piper_model_config="",
            piper_length_scale=1.0,
            system_voice="",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)

        async def _fake_synth(text, **kw):
            assert kw["piper_model"] == "~/m.onnx"
            yield 0, 22050, b"\x00\x01"

        streaming_called = False

        async def _fake_stream(*a, **kw):
            nonlocal streaming_called
            streaming_called = True
            if False:
                yield  # pragma: no cover — make it an async generator

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.streaming_piper_reply", _fake_synth)
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
        payloads = [c.args[1] for c in state.broadcast_ws.call_args_list]
        assert all(payload["audioMime"] == "audio/wav" for payload in payloads)

    @pytest.mark.asyncio
    async def test_put_config_invalid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put(
                "/api/voice/config", data=b"not json", headers={"Content-Type": "application/json"}
            )
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
            # Named explicitly: routing sends every non-Polly provider to the
            # single-file path, so leaving this to MagicMock's auto-attribute
            # would exercise the wrong branch from the one mocked below.
            provider="polly",
            default_voice="Joanna",
            default_engine="neural",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="us-east-1",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)

        # Mock streaming_voice_reply to yield one chunk
        async def mock_stream(*a, **kw):
            yield 0, "Hello", b"\x00\x01\x02"

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.streaming_voice_reply", mock_stream)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.stitch_mp3s", AsyncMock(return_value=None)
        )

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post(
                "/api/voice/synthesize", json={"text": "Hello world", "slot": "s1"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["chunks"] == 1
        state.broadcast_ws.assert_called()

    @pytest.mark.asyncio
    async def test_piper_replay_is_encoded_off_the_event_loop(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_voice

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            chat_voice,
            "_vc",
            MagicMock(
                provider="piper",
                piper_binary="",
                piper_model="~/m.onnx",
                piper_model_config="",
                piper_length_scale=1.0,
            ),
        )

        async def stream(*args, **kwargs):
            yield 0, 22050, b"\x01\x00"
            yield 1, 22050, b"\x02\x00"

        monkeypatch.setattr(chat_voice, "streaming_piper_reply", stream)
        loop_thread = threading.get_ident()
        replay_threads = []
        real_wav = chat_voice.pcm_to_wav

        def watch_wav(pcm, rate):
            if len(pcm) == 4:
                replay_threads.append(threading.get_ident())
            return real_wav(pcm, rate)

        monkeypatch.setattr(chat_voice, "pcm_to_wav", watch_wav)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "hello", "slot": "s1"})
            assert resp.status == 200
        assert replay_threads and loop_thread not in replay_threads
        assert [call.args[0] for call in state.broadcast_ws.call_args_list] == [
            "voice_chunk",
            "voice_chunk",
            "voice_complete",
        ]

    @pytest.mark.asyncio
    async def test_the_polly_chunks_are_written_and_read_off_the_event_loop(
        self, tmp_path, monkeypatch
    ):
        """Same rule, streaming path — and it is the worse of the two.

        The chunk spill runs once per SENTENCE inside the streaming loop, so a
        long reply blocks the loop repeatedly, and the stitched mp3 is then read
        whole on top of that.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            provider="polly",
            default_voice="Joanna",
            default_engine="neural",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="us-east-1",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)

        async def _mock_stream(*a, **kw):
            yield 0, "Hello", b"\x00\x01\x02"
            yield 1, "Again", b"\x03\x04\x05"

        final = tmp_path / "final.mp3"
        final.write_bytes(b"ID3stitched-audio")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.streaming_voice_reply", _mock_stream)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.stitch_mp3s", AsyncMock(return_value=str(final))
        )

        loop_thread = threading.get_ident()
        audio_io_threads: list[int] = []
        real_open = builtins.open

        def _watch_open(file, *args, **kwargs):
            # Both the per-sentence chunk spill and the stitched read are `.mp3`;
            # the chunk path is chosen by mkstemp, so match on the suffix rather
            # than on a path the test cannot know in advance.
            if str(file).endswith(".mp3"):
                audio_io_threads.append(threading.get_ident())
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _watch_open)

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post(
                "/api/voice/synthesize", json={"text": "Hello. Again.", "slot": "s1"}
            )
            assert resp.status == 200
            assert (await resp.json())["chunks"] == 2

        # Two chunk writes plus the stitched read: the probe must have seen all
        # three, otherwise "not on the loop" would be vacuously true.
        assert (
            len(audio_io_threads) == 3
        ), f"expected 2 chunk writes + 1 stitched read, saw {len(audio_io_threads)}"
        assert loop_thread not in audio_io_threads, (
            "synthesized audio was written or read on the gateway event loop "
            f"(thread {loop_thread})"
        )

    @pytest.mark.asyncio
    async def test_synthesize_exception_returns_502_and_broadcasts_error(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            # Named explicitly: routing sends every non-Polly provider to the
            # single-file path, so leaving this to MagicMock's auto-attribute
            # would exercise the wrong branch from the one mocked below.
            provider="polly",
            default_voice="Joanna",
            default_engine="neural",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="us-east-1",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)

        # Mock streaming_voice_reply to raise an exception
        async def mock_stream_error(*a, **kw):
            raise RuntimeError("Polly synthesis failed")
            yield  # noqa: unreachable - makes this a generator

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.streaming_voice_reply", mock_stream_error
        )

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "Hello", "slot": "s1"})
            assert resp.status == 502
            data = await resp.json()
            assert data["ok"] is False
            assert "error" in data
        # Verify voice_error was broadcast
        state.broadcast_ws.assert_called()
        call_args = state.broadcast_ws.call_args
        assert call_args[0][0] == "voice_error"
        assert call_args[0][1]["slot"] == "s1"


def _consent_to_polly(*, profile: str, region: str) -> None:
    """Record operator consent for Polly under one profile+region pair."""
    from kiro_crew import aws_consent

    aws_consent.record_grant(
        aws_consent.SERVICE_POLLY,
        profile=profile,
        region=region,
        account="111122223333",
        arn="arn:aws:iam::111122223333:user/test",
        granted_at="2026-08-21T00:00:00+00:00",
    )


class TestVoiceVoices:
    @pytest.fixture(autouse=True)
    def _polly_consented(self, tmp_path_factory, monkeypatch):
        """Consent for Polly under the default profile+region, throwaway home.

        The voice catalogue is an ``aws polly describe-voices`` call, so it is
        gated like every other billable Polly request. Cases that assert the
        REFUSAL live in ``test_aws_consent.py``; these cases are about the
        catalogue's own success and error handling, so they consent first.

        Exactly ONE grant exists per service (a grant records the profile+region
        it was given for), so a case using a different pair records its own --
        see ``test_voices_returns_list``.
        """
        home = tmp_path_factory.mktemp("voices-consent-home")
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        from kiro_crew.config.loader import config_dir

        config_dir().mkdir(parents=True, exist_ok=True)
        _consent_to_polly(profile="", region="")
        # The gate also verifies the LIVE account, which would spawn the AWS CLI
        # behind this class's `resolve_polly_cli` stub. These cases are about
        # the catalogue, so return a matching identity.
        from kiro_crew import aws_consent

        async def _probe(_profile, _region, *, use_cache=True):
            return aws_consent.Identity(ok=True, account="111122223333")

        monkeypatch.setattr(aws_consent, "probe_identity", _probe)

    @pytest.mark.asyncio
    async def test_voices_returns_list(self, tmp_path, monkeypatch):
        """Test successful voice listing."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(provider="polly", aws_profile="polly", region="us-east-1")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        # This case uses a NON-default profile+region, and a grant is keyed on
        # both, so the class fixture's grant does not cover it.
        _consent_to_polly(profile="polly", region="us-east-1")
        # Reset cache
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache", None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache_ts", 0)

        mock_data = json.dumps(
            {
                "Voices": [
                    {
                        "Id": "Takumi",
                        "Name": "Takumi",
                        "LanguageName": "Japanese",
                        "LanguageCode": "ja-JP",
                        "Gender": "Male",
                        "SupportedEngines": ["neural", "standard"],
                    },
                    {
                        "Id": "Mizuki",
                        "Name": "Mizuki",
                        "LanguageName": "Japanese",
                        "LanguageCode": "ja-JP",
                        "Gender": "Female",
                        "SupportedEngines": ["standard"],
                    },
                ]
            }
        )

        async def mock_exec(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 0

            async def comm():
                return mock_data.encode(), b""

            proc.communicate = comm
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_exec)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.resolve_polly_cli", lambda: "/usr/local/bin/aws"
        )

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
        mock_vc = MagicMock(provider="polly", aws_profile="", region="")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        cached = [
            {
                "id": "Ruth",
                "name": "Ruth",
                "language": "English",
                "languageCode": "en-US",
                "gender": "Female",
                "engines": ["neural"],
            }
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
        mock_vc = MagicMock(provider="polly", aws_profile="", region="")
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
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.resolve_polly_cli", lambda: "/usr/local/bin/aws"
        )

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
        mock_vc = MagicMock(provider="polly", aws_profile="", region="")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache", None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache_ts", 0)

        async def mock_exec(*args, **kwargs):
            proc = MagicMock()
            # First await (under wait_for) times out; the second (the reap
            # after kill) drains the pipes and returns.
            proc.communicate = AsyncMock(side_effect=[asyncio.TimeoutError(), (b"", b"")])
            proc.kill = MagicMock()
            proc.wait = AsyncMock()
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_exec)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.resolve_polly_cli", lambda: "/usr/local/bin/aws"
        )

        from kiro_crew.dashboard.chat_voice import api_voice_voices

        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/voice/voices", api_voice_voices)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/voice/voices")
            assert resp.status == 504

    @pytest.mark.asyncio
    async def test_voices_timeout_reaps_child_via_communicate_not_wait(self, tmp_path, monkeypatch):
        """After a timeout kills the describe-voices child, the cleanup must
        call ``communicate()`` -- not ``wait()`` -- so that PIPE buffers are
        drained. A child blocked writing to a full stderr PIPE would hang the
        request handler if only ``wait()`` were used (#5975)."""
        import asyncio

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(provider="polly", aws_profile="", region="")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache", None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache_ts", 0)

        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=[asyncio.TimeoutError(), (b"", b"")])
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        async def mock_exec(*args, **kwargs):
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_exec)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.resolve_polly_cli", lambda: "/usr/local/bin/aws"
        )

        from kiro_crew.dashboard.chat_voice import api_voice_voices

        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/voice/voices", api_voice_voices)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/voice/voices")
            assert resp.status == 504

        proc.kill.assert_called_once()
        # The critical pin: reap via communicate(), not wait(). The handler
        # awaits communicate once under wait_for; the reap must award a
        # SECOND await, and wait() must never be touched. (A bare
        # ``communicate.assert_awaited()`` would pass even against a
        # wait()-based reap, so it must be this count/not_awaited shape.)
        assert proc.communicate.await_count == 2
        proc.wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_voices_aws_not_found(self, tmp_path, monkeypatch):
        """aws CLI absent from PATH → 200 with empty list, no subprocess spawn."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(provider="polly", aws_profile="", region="")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache", None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache_ts", 0)

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.resolve_polly_cli", lambda: None)
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
        mock_vc = MagicMock(provider="polly", aws_profile="", region="")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache", None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._voices_cache_ts", 0)

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.resolve_polly_cli", lambda: "/usr/local/bin/aws"
        )

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


class TestVoiceSystemVoices:
    def _app(self, tmp_path):
        from kiro_crew.dashboard.chat_voice import api_voice_system_voices

        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/voice/system-voices", api_voice_system_voices)
        return app

    @pytest.fixture(autouse=True)
    def _clear_cache(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._system_voices_cache", None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._system_voices_cache_ts", 0)

    @pytest.mark.asyncio
    async def test_no_engine_reports_unavailable_without_probing(self, tmp_path, monkeypatch):
        """A host with no engine answers before spawning anything.

        The panel needs this distinct from "engine present, no voices": the
        first needs an install, the second does not.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.resolve_system_tts_async", AsyncMock(return_value=None)
        )
        probe = AsyncMock(return_value=[])
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.list_system_voices", probe)

        async with TestClient(TestServer(self._app(tmp_path))) as client:
            resp = await client.get("/api/voice/system-voices")
            assert resp.status == 200
            assert await resp.json() == {"available": False, "voices": []}
        probe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lists_voices_and_names_the_engine(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.resolve_system_tts_async",
            AsyncMock(return_value=("say", "/usr/bin/say")),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.list_system_voices",
            AsyncMock(return_value=[{"id": "Alex", "name": "Alex", "language": "en-US"}]),
        )

        async with TestClient(TestServer(self._app(tmp_path))) as client:
            resp = await client.get("/api/voice/system-voices")
            assert resp.status == 200
            data = await resp.json()
        assert data["available"] is True
        assert data["voices"] == [{"id": "Alex", "name": "Alex", "language": "en-US"}]
        # The response carries only what the panel reads; naming the engine
        # would be public HTTP surface with no consumer.
        assert "engine" not in data

    @pytest.mark.asyncio
    async def test_result_is_cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.resolve_system_tts_async",
            AsyncMock(return_value=("say", "/usr/bin/say")),
        )
        probe = AsyncMock(return_value=[])
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.list_system_voices", probe)

        async with TestClient(TestServer(self._app(tmp_path))) as client:
            await client.get("/api/voice/system-voices")
            await client.get("/api/voice/system-voices")
        # An empty list is cached too: it is stable for the life of the install,
        # and re-probing would cost a subprocess per panel visit.
        assert probe.await_count == 1

    @pytest.mark.asyncio
    async def test_probe_failure_returns_502(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.resolve_system_tts_async",
            AsyncMock(return_value=("say", "/usr/bin/say")),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.list_system_voices",
            AsyncMock(side_effect=RuntimeError("boom")),
        )

        async with TestClient(TestServer(self._app(tmp_path))) as client:
            resp = await client.get("/api/voice/system-voices")
            assert resp.status == 502
            body = await resp.json()
            assert body["error"] == "Failed to retrieve voices"
            # The dashboard renders `error` verbatim into a localized UI, so the
            # machine-readable code is the part it can actually translate.
            assert body["code"] == "system_voices_probe_failed"


class TestSynthesizeProviderRouting:
    """Only Polly can reach the paid sentence stream.

    Piper has its local PCM stream; system and unknown future providers retain
    the provider-aware single-file path rather than falling through to AWS.
    """

    def _mock_vc(self, provider: str):
        return MagicMock(
            provider=provider,
            default_voice="Ruth",
            default_engine="generative",
            default_rate="110%",
            default_pitch="0%",
            aws_profile="",
            region="",
            piper_binary="",
            piper_model="",
            piper_model_config="",
            piper_length_scale=1.0,
            system_voice="Alex",
        )

    @pytest.mark.asyncio
    async def test_system_provider_uses_the_single_file_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", self._mock_vc("system"))

        wav = tmp_path / "out.wav"
        wav.write_bytes(b"RIFF" + b"x" * 200)
        synth = AsyncMock(return_value=str(wav))
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.synthesize_speech", synth)

        async def unreachable(*a, **kw):
            raise AssertionError("Polly streaming must not run for a local provider")
            yield  # noqa: unreachable - keeps this an async generator

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.streaming_voice_reply", unreachable)

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        app = _make_voice_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/voice/synthesize",
                json={"text": "Hello", "slot": "s1", "request_id": "native-success"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["chunks"] == 1
            assert body["request_id"] == "native-success"

        kwargs = synth.await_args.kwargs
        assert kwargs["provider"] == "system"
        assert kwargs["system_voice"] == "Alex"
        # The built-in engine reads speed from the shared rate percentage, so
        # the configured value has to reach it.
        assert kwargs["rate"] == "110%"
        mimes = [
            call[0][1].get("audioMime")
            for call in state.broadcast_ws.call_args_list
            if call[0][0] in ("voice_chunk", "voice_complete")
        ]
        assert mimes == ["audio/wav", "audio/wav"]
        assert all(
            call.args[1]["request_id"] == "native-success" and call.args[1]["slot"] == "s1"
            for call in state.broadcast_ws.call_args_list
        )
        assert not wav.exists()

    @pytest.mark.asyncio
    async def test_system_synthesis_is_cancelled_by_its_request_identity(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import chat_voice

        monkeypatch.setattr(chat_voice, "_vc", self._mock_vc("system"))
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def synthesize(*_args, **_kwargs):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                stopped.set()

        monkeypatch.setattr(chat_voice, "synthesize_speech", synthesize)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        app = _make_voice_app(state)
        async with TestClient(TestServer(app)) as client:
            identity = {"slot": "s1", "request_id": "native-stop"}
            pending = asyncio.create_task(
                client.post("/api/voice/synthesize", json={**identity, "text": "Hello"})
            )
            try:
                await asyncio.wait_for(started.wait(), 5)
                response = await client.post("/api/voice/cancel", json=identity)
                assert response.status == 200
                await asyncio.wait_for(stopped.wait(), 5)
                assert not app[chat_voice._VOICE_REQUESTS].tasks
                state.broadcast_ws.assert_not_called()
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("resolved", "expect_in", "expect_not_in"),
        [
            (None, "espeak-ng", "produced no audio"),
            (("say", "/usr/bin/say"), "produced no audio", "espeak-ng"),
        ],
        ids=["engine-absent", "engine-present-but-failed"],
    )
    async def test_system_failure_remedy_matches_why_it_failed(
        self, tmp_path, monkeypatch, resolved, expect_in, expect_not_in
    ):
        """The install remedy is only correct when the engine is actually absent.

        Synthesis also returns None with an engine present — a persisted
        ``system_voice`` the engine rejects, a timeout, a sandbox refusal — and
        telling that user to install espeak-ng sends them to fix something that
        is not broken. So the branch is chosen by PROBING, and this pins both
        sides: an absent engine must still get the install remedy, and a present
        one must not.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", self._mock_vc("system"))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.synthesize_speech",
            AsyncMock(return_value=None),
        )
        # Patch where the name is USED, not where it is defined. chat_voice imports
        # it at module scope, so patching the definition site leaves this call
        # bound to the real probe -- the sibling tests above already patch here.
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_voice.resolve_system_tts_async",
            AsyncMock(return_value=resolved),
        )

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        app = _make_voice_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/voice/synthesize",
                json={"text": "Hello", "slot": "s1", "request_id": "native-failed"},
            )
            assert resp.status == 502
            body = await resp.json()
            err = body["error"]
            assert body["request_id"] == "native-failed"
            assert body["code"] == (
                "voice_unavailable" if resolved is None else "voice_synthesis_failed"
            )
        state.broadcast_ws.assert_called_once_with(
            "voice_error",
            {"slot": "s1", "request_id": "native-failed", "error": err, "code": body["code"]},
        )
        assert expect_in in err
        assert expect_not_in not in err
        # A remedy naming the wrong provider costs the user the whole debugging
        # session, so the two local providers must not share one message.
        assert "piper" not in err.lower()


def test_every_body_field_the_config_put_reads_goes_through_a_validator():
    """No value from the request body may reach `_vc` via a bare ``str()``.

    This is a RATCHET, not another point fix. Four consecutive review rounds
    landed the same class of finding on this one handler -- a non-object body, a
    wrong-typed ``system_voice``, a torn write, then a wrong-typed ``voice`` --
    because each round fixed the field that was reported and left its siblings on
    ``str()``. ``str({})`` is ``"{}"``: a syntactically valid voice name or path
    that no engine can satisfy, so synthesis goes silent while the stored config
    looks populated.

    The invariant that makes the whole class unreachable is structural rather than
    per-field: every ``body[...]`` read in ``api_voice_config`` must be an
    argument to a validator that owns a documented contract, or be guarded by an
    ``isinstance`` + membership test. A new field added on a bare ``str()`` or
    ``bool()`` fails here instead of shipping and being found by a reviewer one
    round later.
    """
    import ast
    import inspect

    from kiro_crew.dashboard import chat_voice

    # Note what is NOT here: bare ``bool``. It never raises, which reads as safe,
    # but bool("false") is True -- a caller sending the string "false" would
    # persist the opposite setting. Totality is not truth-preservation, and that
    # mistaken reasoning is exactly what this list previously encoded.
    SANCTIONED = {
        "validated_config_bool",
        "validated_config_string",
        "validate_length_scale",
        "_validate_rate",
        "_validate_pitch",
    }

    source = inspect.getsource(chat_voice.api_voice_config)
    tree = ast.parse(textwrap.dedent(source))

    # Every Call node's arguments, mapped to the callee name, so a body[...] read
    # can be attributed to the call that wraps it.
    wrapped: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            for arg in node.args:
                for inner in ast.walk(arg):
                    wrapped[id(inner)] = name or "<unknown>"

    # `x in VALID_*` / isinstance(...) guards protect provider and engine, which
    # are validated by membership rather than by coercion.
    guarded: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.In):
            target = getattr(node.comparators[0], "id", "")
            if target.startswith("VALID_"):
                for inner in ast.walk(node.left):
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                        guarded.add(inner.value)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        if getattr(node.value, "id", None) != "body":
            continue
        key = getattr(node.slice, "value", None)
        if not isinstance(key, str):
            continue  # a loop variable; the loop body is checked by its own call
        if key in guarded:
            continue
        callee = wrapped.get(id(node))
        if callee not in SANCTIONED:
            offenders.append(f"body[{key!r}] reaches _vc via {callee or 'no call'}")

    assert not offenders, "every config-PUT field must go through a validator; found: " + "; ".join(
        sorted(offenders)
    )


class TestSandboxRefusalReachesTheUser:
    """A fail-closed sandbox must reach the CHAT SURFACE, not only the log.

    The remedy prose lives in ``SandboxUnavailableError`` and nowhere else. A
    provider that collapses it into a generic "no audio" leaves the one fact that
    would fix the host — that the sandbox refused, and what to do about it —
    discoverable only by reading the gateway log.
    """

    _PROSE = "SANDBOX-REMEDY-SENTINEL: set agent.sandbox_allow_unsandboxed_exec=true"

    @pytest.mark.asyncio
    async def test_piper_refusal_is_relayed_with_the_sandbox_prose(self, tmp_path, monkeypatch):
        """The Piper (non-streaming) branch reports the refusal, not a config guess.

        The pre-existing message blamed the piper binary and model path, which for
        a sandbox refusal sends the operator to check two settings that are both
        already correct.
        """
        from kiro_crew.sandbox import SandboxUnavailableError

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            provider="system",
            piper_binary="/usr/bin/piper",
            piper_model="/m.onnx",
            piper_model_config="",
            piper_length_scale=1.0,
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)

        async def refuse(*a, **kw):
            raise SandboxUnavailableError(self._PROSE, "no_backend", "not Linux")

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.synthesize_speech", refuse)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.notify = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post(
                "/api/voice/synthesize", json={"text": "hi", "slot": "s1", "request_id": "rq1"}
            )
            assert resp.status == 502
            body = await resp.json()
            # Auto-speak sends one request per sentence, and a caller picks its own
            # slot. A DIFFERENT slot must still not raise a second notification:
            # the refusal is a host-level property, so the throttle keys on the
            # sandbox kind alone and request data never enters a long-lived key.
            await client.post("/api/voice/synthesize", json={"text": "again", "slot": "s2"})
        assert self._PROSE in body["error"], "the sandbox's own remedy must be relayed"
        assert "piper binary" not in body["error"], "must not blame the piper config"
        # A localized UI cannot translate the relayed prose, so the refusal also
        # carries a machine-readable id -- and it names the KIND, since that is
        # what decides which remedy the prose describes.
        assert body["code"] == "sandbox_no_backend"
        # The refusal reaches a surface the user actually sees. Neither the 502 nor
        # the voice_error broadcast does: both auto-speak call sites discard the
        # rejected request, and no dashboard code consumes "voice_error".
        assert state.notify.call_count == 1, "one notification per sandbox kind, not per slot"
        assert self._PROSE in state.notify.call_args[0][2], "the remedy must survive"
        # The dashboard learns about it too, not just the HTTP caller.
        errors = [c for c in state.broadcast_ws.call_args_list if c[0][0] == "voice_error"]
        assert errors, "a voice_error must be broadcast"
        payload = errors[0][0][1]
        assert self._PROSE in payload["error"]
        # The dashboard's voice_error handler drops any event without a
        # request_id and keys its generic failure off `code`, so a broadcast
        # missing either is discarded before it reaches that surface at all.
        assert payload["request_id"] == "rq1"
        assert payload["code"] == "sandbox_no_backend"

    @pytest.mark.asyncio
    async def test_polly_refusal_is_relayed_with_the_sandbox_prose(self, tmp_path, monkeypatch):
        """The Polly (streaming) branch relays it too.

        Covered explicitly because the two providers take DIFFERENT endpoint
        branches -- Piper is one local WAV, Polly is sentence-chunked -- so fixing
        only the branch named in the report would have left the other silent.
        """
        from kiro_crew.sandbox import SandboxUnavailableError

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            provider="polly",
            default_voice="Joanna",
            default_engine="neural",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="us-east-1",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)

        async def refuse(*a, **kw):
            raise SandboxUnavailableError(self._PROSE, "no_backend", "not Linux")
            yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.streaming_voice_reply", refuse)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.notify = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "hi", "slot": "s1"})
            # Identical to the Piper branch, not merely "an error": Polly reaches
            # the sandbox through the streaming path, and falling to the generic
            # handler there would report 500 with no notification -- the remedy
            # invisible on a Polly host, which is the defect being fixed.
            assert resp.status == 502
            body = await resp.json()
        assert self._PROSE in body["error"], "the sandbox's own remedy must be relayed"
        assert body["code"] == "sandbox_no_backend"
        assert state.notify.call_count == 1, "the user-visible surface must fire here too"
        assert self._PROSE in state.notify.call_args[0][2]

    @pytest.mark.asyncio
    async def test_a_transient_refusal_never_gains_the_opt_in_advice(self, tmp_path, monkeypatch):
        """The endpoint must add no remedy of its own.

        ``exc.kind`` decides the advice: for ``transient`` the sandbox layer says
        retry and explicitly says callers must NOT advise disabling isolation, and
        ``foreign_sandbox`` points at a kiro-cli setting. An endpoint that pasted
        in the ``allow_unsandboxed_exec`` key would tell an operator to
        permanently drop the sandbox to work around momentary resource pressure --
        so this asserts the key is ABSENT when the sandbox did not supply it.
        """
        from kiro_crew.sandbox import SandboxUnavailableError

        transient = "This looks TRANSIENT (momentary resource pressure). Do NOT disable; retry."
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            provider="system",
            piper_binary="/usr/bin/piper",
            piper_model="/m.onnx",
            piper_model_config="",
            piper_length_scale=1.0,
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)

        async def refuse(*a, **kw):
            raise SandboxUnavailableError(transient, "transient", "fork: EAGAIN")

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.synthesize_speech", refuse)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        # Mocked like the sibling refusal tests: the real notification bus hands the
        # write to an executor, and a backlogged worker can land it after pytest has
        # removed `tmp_path` -- recreating the directory as residue.
        state.notify = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "hi", "slot": "s1"})
            body = await resp.json()
        assert transient in body["error"]
        assert "sandbox_allow_unsandboxed_exec" not in body["error"]
        # The code tracks the kind, so a client can tell "retry shortly" apart
        # from "this host will never synthesize" without parsing the prose.
        assert body["code"] == "sandbox_transient"
