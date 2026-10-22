"""The ``speech`` platform seam, Phase 2: dispatch and capabilities reach a registered provider.

A fake ``acme`` provider registered on a test context must be accepted by config,
listed with its ``check()`` status in both capability responses, handed every
recorded clip and every synthesis, and allowed to stream only when it says it can.
Built-in names keep their own branches, and a config loaded before boot never
saves a registered name back as ``off``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import unittest.mock
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from stt_stream_helpers import _cfg, _make_app, _recv_json

from kiro_crew import speech_providers, transcribe, voice_reply
from kiro_crew.config import sections_stt
from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache, config_path
from kiro_crew.config.sections_stt import SttConfig
from kiro_crew.platform import bootstrap as bootstrap_mod
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.interfaces import (
    PROVIDER_ACTION_LOGIN,
    AudioChunk,
    ProviderAction,
    ProviderStatus,
)
from kiro_crew.testing.wait import await_until

_SIGN_IN = ProviderAction(PROVIDER_ACTION_LOGIN, "Sign in to Acme")


class _Session:
    """An ``SttStreamSession`` that answers each fed frame with one final."""

    def __init__(self) -> None:
        self.fed: list[bytes] = []
        self._events: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def start(self) -> Optional[str]:
        return None

    async def feed(self, pcm: bytes) -> bool:
        self.fed.append(pcm)
        await self._events.put({"type": "final", "text": f"heard {len(pcm)} bytes"})
        return True

    async def events(self):
        while (event := await self._events.get()) is not None:
            yield event

    async def finish(self) -> None:
        await self._events.put(None)

    async def close(self) -> None:
        self.closed = True


class _AcmeStt:
    name = "acme"
    label = "Acme Cloud"

    def __init__(self, *, streaming: bool = True, status: Optional[ProviderStatus] = None) -> None:
        self.streaming = streaming
        self.status = status or ProviderStatus(True)
        self.clips: list[tuple[str, Any]] = []
        self.sessions: list[_Session] = []

    async def check(self, stt_config: Any) -> ProviderStatus:
        return self.status

    async def transcribe_file(self, audio_path: str, stt_config: Any) -> Optional[str]:
        self.clips.append((audio_path, stt_config))
        return "hello from acme"

    def open_stream(self, stt_config: Any) -> _Session:
        session = _Session()
        self.sessions.append(session)
        return session


def _wav(frames: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(frames)
    return buf.getvalue()


class _AcmeTts:
    name = "acme"
    label = "Acme Voice"

    def __init__(self, *, status: Optional[ProviderStatus] = None) -> None:
        self.status = status or ProviderStatus(True)
        self.spoken: list[tuple[str, dict]] = []

    async def check(self, voice_config: Any) -> ProviderStatus:
        return self.status

    async def synthesize(self, text: str, voice_config: Any):
        self.spoken.append((text, voice_config))
        yield AudioChunk(_wav(b"\x01\x00" * 4), "audio/wav", "first")
        yield AudioChunk(_wav(b"\x02\x00" * 4), "audio/wav", "second")


class _Registry:
    def __init__(self, stt: tuple = (), tts: tuple = ()) -> None:
        self._stt = stt
        self._tts = tts

    def stt_providers(self) -> tuple:
        return self._stt

    def tts_providers(self) -> tuple:
        return self._tts

    def default_stt(self) -> Optional[str]:
        return None

    def default_tts(self) -> Optional[str]:
        return None


@pytest.fixture(autouse=True)
def _clean_state():
    reset_context()
    _invalidate_config_cache()
    sections_stt._WARNED_STT_PROVIDERS.clear()
    sections_stt._DEFERRED_STT_PROVIDERS.clear()
    speech_providers._reset_owner_loop()
    yield
    reset_context()
    _invalidate_config_cache()
    sections_stt._WARNED_STT_PROVIDERS.clear()
    sections_stt._DEFERRED_STT_PROVIDERS.clear()
    speech_providers._reset_owner_loop()
    bootstrap_mod._reset_boot_state()


def _install(stt: tuple = (), tts: tuple = ()) -> None:
    ctx = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(ctx, speech=_Registry(stt, tts)))


def _write_config(data: dict) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    _invalidate_config_cache()
    return path


# ── config accepts it ────────────────────────────────────────────────────────


class TestConfigAcceptsIt:
    def test_the_loader_and_the_tts_resolver_keep_it(self) -> None:
        _install(stt=(_AcmeStt(),), tts=(_AcmeTts(),))
        _write_config({"stt": {"provider": "acme"}})
        assert KiroCrewConfig.load().stt.provider == "acme"
        assert voice_reply.resolve_configured_provider({"provider": "acme"}) == "acme"

    @pytest.mark.asyncio
    async def test_the_stt_put_stores_it(self) -> None:
        from kiro_crew.dashboard.handlers import core as core_mod

        _install(stt=(_AcmeStt(),))
        path = _write_config({"stt": {"provider": "local"}})
        app = web.Application()
        app["state"] = SimpleNamespace(owner_id="owner-1")
        app.router.add_route("*", "/api/config/stt", core_mod.api_stt_config)
        with unittest.mock.patch.object(core_mod, "_stt_prereq_commands", lambda _p: []):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put("/api/config/stt", json={"provider": "acme"})
                assert resp.status == 200
        assert json.loads(path.read_text())["stt"]["provider"] == "acme"

    @pytest.mark.asyncio
    async def test_the_voice_put_stores_it(self, tmp_path, monkeypatch) -> None:
        _install(tts=(_AcmeTts(),))
        path = _write_config({})
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.config_path", lambda: path)
        vc = SimpleNamespace(provider="system")
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", vc)
        async with TestClient(TestServer(_voice_app(_make_state(tmp_path)))) as client:
            resp = await client.put("/api/voice/config", json={"provider": "acme"})
            assert resp.status == 200
        assert json.loads(path.read_text())["voice_reply"]["provider"] == "acme"
        assert vc.provider == "acme"


# ── capability responses ─────────────────────────────────────────────────────


def _voice_app(state) -> web.Application:
    from kiro_crew.dashboard.chat_voice import (
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
    return app


_ROW_KEYS = {"name", "label", "builtin", "available", "reason", "action", "error"}


class TestCapabilityResponses:
    @pytest.mark.asyncio
    async def test_stt_rows_list_builtins_first_then_acme_then_off(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import core as core_mod

        acme = _AcmeStt(status=ProviderStatus(False, "Acme needs a sign-in", action=_SIGN_IN))
        _install(stt=(acme,))
        _write_config({"stt": {"provider": "local"}})
        monkeypatch.setattr(core_mod, "_stt_prereq_commands", lambda _p: [])
        monkeypatch.setattr(
            core_mod, "availability_detail", lambda _cfg: core_mod.stt.Availability(True)
        )
        app = web.Application()
        app["state"] = SimpleNamespace(owner_id="owner-1")
        app.router.add_route("*", "/api/config/stt", core_mod.api_stt_config)
        async with TestClient(TestServer(app)) as client:
            body = await (await client.get("/api/config/stt")).json()
        rows = body["providers"]
        assert all(set(row) == _ROW_KEYS for row in rows)
        names = [row["name"] for row in rows]
        assert names[-2:] == ["acme", "off"]
        assert [n for n in names if n not in ("acme", "off")] == [
            n for n in ("local", "apple", "transcribe") if n in names
        ]
        assert all(row["builtin"] for row in rows if row["name"] != "acme")
        assert next(row for row in rows if row["name"] == "acme") == {
            "name": "acme",
            "label": "Acme Cloud",
            "builtin": False,
            "available": False,
            "reason": "Acme needs a sign-in",
            "action": {"kind": "login", "label": "Sign in to Acme"},
            "error": False,
        }
        assert not any(row["error"] for row in rows)
        assert "acme" in body["streaming_providers"]

    @pytest.mark.asyncio
    async def test_voice_rows_list_builtins_first_then_acme(self, tmp_path, monkeypatch) -> None:
        acme = _AcmeTts(status=ProviderStatus(False, "Acme needs a sign-in", action=_SIGN_IN))
        _install(tts=(acme,))
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.is_available", lambda *a, **k: True)
        async with TestClient(TestServer(_voice_app(_make_state(tmp_path)))) as client:
            rows = (await (await client.get("/api/voice/config")).json())["providers"]
        assert all(set(row) == _ROW_KEYS for row in rows)
        assert [row["name"] for row in rows] == ["system", "piper", "polly", "acme"]
        assert [row["builtin"] for row in rows] == [True, True, True, False]
        assert rows[-1]["available"] is False
        assert rows[-1]["reason"] == "Acme needs a sign-in"
        assert rows[-1]["action"] == {"kind": "login", "label": "Sign in to Acme"}
        assert [row["error"] for row in rows] == [False, False, False, False]

    @pytest.mark.asyncio
    async def test_a_builtin_probe_that_raises_is_an_error(self, tmp_path, monkeypatch) -> None:
        def raises(*a, **k):
            raise RuntimeError("probe broke")

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.is_available", raises)
        async with TestClient(TestServer(_voice_app(_make_state(tmp_path)))) as client:
            rows = (await (await client.get("/api/voice/config")).json())["providers"]
        assert [(r["available"], r["error"]) for r in rows] == [(False, True)] * 3

    @pytest.mark.asyncio
    async def test_a_check_that_raises_or_hangs_is_unavailable(self, monkeypatch) -> None:
        class _Raises(_AcmeStt):
            async def check(self, stt_config):
                raise RuntimeError("down")

        class _Hangs(_AcmeStt):
            name = "slow"

            async def check(self, stt_config):
                await asyncio.Event().wait()

        monkeypatch.setattr(speech_providers, "CHECK_TIMEOUT_SECS", 0.01)
        rows = await speech_providers.registered_entries(
            (_Raises(), _Hangs()), lambda provider: SttConfig()
        )
        assert [(r["available"], r["reason"], r["error"]) for r in rows] == [
            (False, "the provider check failed", True),
            (False, "the provider did not answer in time", True),
        ]

    @pytest.mark.asyncio
    async def test_a_plain_unavailable_is_not_an_error(self) -> None:
        """Even a provider that claims ``error`` itself: only core sets it."""
        status = SimpleNamespace(available=False, reason="Acme needs a sign-in", error=True)

        class _Unavailable(_AcmeStt):
            async def check(self, stt_config):
                return status

        (row,) = await speech_providers.registered_entries(
            (_Unavailable(),), lambda provider: SttConfig()
        )
        assert (row["available"], row["reason"], row["error"]) == (
            False,
            "Acme needs a sign-in",
            False,
        )

    def test_a_malformed_action_is_dropped(self) -> None:
        status = speech_providers._coerce_status(
            SimpleNamespace(available=False, reason="x", action=SimpleNamespace(kind="open"))
        )
        assert status.action is None
        assert speech_providers._coerce_status(SimpleNamespace(available="yes")).available is False


# ── dispatch: STT ────────────────────────────────────────────────────────────


class TestSttDispatch:
    def test_availability_asks_its_check(self) -> None:
        acme = _AcmeStt(status=ProviderStatus(False, "Acme needs a sign-in", action=_SIGN_IN))
        _install(stt=(acme,))
        with unittest.mock.patch.object(transcribe.stt, "availability") as local:
            detail = transcribe.availability_detail(SttConfig(provider="acme"))
        local.assert_not_called()
        assert (detail.ok, detail.code, detail.detail) == (
            False,
            transcribe.CODE_PROVIDER_UNAVAILABLE,
            "Acme needs a sign-in",
        )
        acme.status = ProviderStatus(True)
        assert transcribe.is_available(SttConfig(provider="acme")) is True

    def test_transcribe_file_receives_the_clip(self, tmp_path) -> None:
        acme = _AcmeStt()
        _install(stt=(acme,))
        clip = tmp_path / "clip.wav"
        clip.write_bytes(b"RIFF")
        cfg = SttConfig(provider="acme")
        with unittest.mock.patch.object(transcribe, "_transcribe_local") as local:
            assert asyncio.run(transcribe.transcribe_audio(str(clip), cfg)) == "hello from acme"
        local.assert_not_called()
        assert acme.clips == [(str(clip), cfg)]

    def test_a_provider_that_raises_answers_none(self, tmp_path) -> None:
        class _Raises(_AcmeStt):
            async def transcribe_file(self, audio_path, stt_config):
                raise RuntimeError("down")

        _install(stt=(_Raises(),))
        clip = tmp_path / "clip.wav"
        clip.write_bytes(b"RIFF")
        assert (
            asyncio.run(transcribe.transcribe_audio(str(clip), SttConfig(provider="acme"))) is None
        )

    @pytest.mark.parametrize("builtin", ["local", "apple", "transcribe"])
    def test_a_builtin_name_never_reaches_the_registry(self, builtin) -> None:
        _install(stt=(_AcmeStt(),))
        assert speech_providers.stt_provider(builtin) is None
        assert speech_providers.stt_provider("off") is None


# ── dispatch: TTS ────────────────────────────────────────────────────────────


class TestTtsDispatch:
    def test_is_available_asks_its_check(self) -> None:
        acme = _AcmeTts(status=ProviderStatus(False, "Acme needs a sign-in"))
        _install(tts=(acme,))
        assert voice_reply.is_available("acme") is False
        acme.status = ProviderStatus(True)
        assert voice_reply.is_available("acme") is True

    def test_synthesize_speech_receives_redacted_text_and_joins_the_audio(self) -> None:
        acme = _AcmeTts()
        _install(tts=(acme,))
        path = asyncio.run(
            voice_reply.synthesize_speech("**Hello** there", provider="acme", voice_id="Ruth")
        )
        try:
            assert path is not None and path.endswith(".wav")
            with wave.open(path, "rb") as w:
                assert w.readframes(w.getnframes()) == b"\x01\x00" * 4 + b"\x02\x00" * 4
        finally:
            Path(path).unlink()
        ((text, voice_config),) = acme.spoken
        assert text == "Hello there"
        assert voice_config["provider"] == "acme" and voice_config["voice"] == "Ruth"

    def test_an_unsupported_format_answers_none(self) -> None:
        class _Ogg(_AcmeTts):
            async def synthesize(self, text, voice_config):
                yield AudioChunk(b"OggS", "audio/ogg")

        _install(tts=(_Ogg(),))
        assert asyncio.run(voice_reply.synthesize_speech("hi", provider="acme")) is None

    @pytest.mark.parametrize("builtin", ["system", "piper", "polly"])
    def test_a_builtin_name_never_reaches_the_registry(self, builtin) -> None:
        _install(tts=(_AcmeTts(),))
        assert speech_providers.tts_provider(builtin) is None

    def test_a_cancel_mid_write_leaves_no_audio_file(self, tmp_path, monkeypatch) -> None:
        """The writer thread outlives a cancelled caller; its file must still go."""
        import tempfile
        import threading

        from kiro_crew import voice_reply_registered

        release = threading.Event()
        made: list[str] = []
        signal: dict = {}
        real_mkstemp = tempfile.mkstemp

        def held_mkstemp(suffix: str = ""):
            fd, path = real_mkstemp(suffix=suffix, dir=tmp_path)
            made.append(path)
            signal["loop"].call_soon_threadsafe(signal["started"].set)
            release.wait(30)
            return fd, path

        monkeypatch.setattr(voice_reply_registered.tempfile, "mkstemp", held_mkstemp)

        async def scenario() -> None:
            signal["loop"], signal["started"] = asyncio.get_running_loop(), asyncio.Event()
            task = asyncio.create_task(
                voice_reply_registered._synthesize_registered(_AcmeTts(), "hi", {})
            )
            await await_until(signal["started"], what="the audio writer to start")
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        assert len(made) == 1 and not Path(made[0]).exists()

    @pytest.mark.asyncio
    async def test_the_dashboard_streams_its_chunks(self, tmp_path, monkeypatch) -> None:
        acme = _AcmeTts()
        _install(tts=(acme,))
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        vc = SimpleNamespace(
            provider="acme",
            default_voice="Ruth",
            default_engine="generative",
            default_rate="100%",
            default_pitch="+0%",
            system_voice="",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", vc)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "hello", "slot": "s1"})
            assert resp.status == 200
            assert (await resp.json())["chunks"] == 2
        frames = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        assert [kind for kind, _ in frames] == ["voice_chunk", "voice_chunk", "voice_complete"]
        assert [p["sentence"] for k, p in frames if k == "voice_chunk"] == ["first", "second"]
        assert all(p["audioMime"] == "audio/wav" for _, p in frames)
        assert acme.spoken[0][0] == "hello"


# ── dispatch: streaming ──────────────────────────────────────────────────────


class TestStreaming:
    def _point_at_acme(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg(provider="acme")),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)

    def test_only_a_streaming_provider_joins_the_grant(self) -> None:
        from kiro_crew.dashboard import stt_stream

        _install(stt=(_AcmeStt(streaming=False),))
        assert stt_stream.streaming_providers() == stt_stream._STREAMING_PROVIDERS
        _install(stt=(_AcmeStt(streaming=True),))
        assert stt_stream.streaming_providers() == (*stt_stream._STREAMING_PROVIDERS, "acme")

    @pytest.mark.asyncio
    async def test_a_streaming_provider_runs_the_session(self, monkeypatch) -> None:
        acme = _AcmeStt(streaming=True)
        _install(stt=(acme,))
        self._point_at_acme(monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _recv_json(ws))["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 8)
            assert await _recv_json(ws) == {"type": "final", "text": "heard 16 bytes"}
            await ws.send_json({"type": "stop"})
            await ws.close()
        (session,) = acme.sessions
        assert session.fed == [b"\x00\x01" * 8]

    @pytest.mark.asyncio
    async def test_a_non_streaming_provider_is_refused(self, monkeypatch) -> None:
        acme = _AcmeStt(streaming=False)
        _install(stt=(acme,))
        self._point_at_acme(monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            frame = await _recv_json(ws)
            assert (frame["type"], frame["code"]) == ("error", "stt_streaming_disabled")
            await ws.close()
        assert acme.sessions == []


# ── a pre-boot snapshot never saves a registered name back as off ────────────


class TestPreBootSnapshotIsNeverSavedAsOff:
    """A config loaded without boot resolves a registered name to ``off``; saving keeps the name."""

    def test_a_companion_host_keeps_the_stored_name(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.platform.discovery.plugin_entry_points", lambda: [object()])
        path = _write_config({"stt": {"provider": "acme"}})
        cfg = KiroCrewConfig.load()
        assert cfg.stt.provider == "off"  # no context yet: built-ins only
        cfg.save()
        assert json.loads(path.read_text())["stt"]["provider"] == "acme"

    def test_a_snapshot_taken_before_registration_keeps_it_after_boot(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.platform.discovery.plugin_entry_points", lambda: [])
        path = _write_config({"stt": {"provider": "acme"}})
        cfg = KiroCrewConfig.load()
        assert cfg.stt.provider == "off"
        _install(stt=(_AcmeStt(),))
        cfg.save()
        assert json.loads(path.read_text())["stt"]["provider"] == "acme"

    def test_standalone_still_saves_an_unknown_name_as_off(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.platform.discovery.plugin_entry_points", lambda: [])
        path = _write_config({"stt": {"provider": "acme"}})
        cfg = KiroCrewConfig.load()
        cfg.save()
        assert json.loads(path.read_text())["stt"]["provider"] == "off"


# ── one loop for every check ─────────────────────────────────────────────────


class _LoopBound:
    """A provider whose client is bound to the loop its first call ran on.

    Like an aiohttp or httpx session cached on first use: any later call from
    another loop fails, as those clients do.
    """

    name = "acme"
    streaming = False

    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def _client(self) -> None:
        current = asyncio.get_running_loop()
        if self.loop is None:
            self.loop = current
        elif self.loop is not current:
            raise RuntimeError("client is bound to a different event loop")

    async def check(self, config: Any) -> ProviderStatus:
        self._client()
        return ProviderStatus(True)

    async def transcribe_file(self, audio_path: str, stt_config: Any) -> Optional[str]:
        self._client()
        return "bound"

    async def synthesize(self, text: str, voice_config: Any):
        self._client()
        yield AudioChunk(_wav(b"\x01\x00" * 4), "audio/wav")


class _LoopBoundSession(_Session):
    """A streaming session that uses its provider's loop-bound client."""

    def __init__(self, owner: "_LoopBound") -> None:
        super().__init__()
        self._owner = owner

    async def start(self) -> Optional[str]:
        self._owner._client()
        return None

    async def feed(self, pcm: bytes) -> bool:
        self._owner._client()
        return await super().feed(pcm)


class _LoopBoundStreaming(_LoopBound):
    streaming = True

    def open_stream(self, stt_config: Any) -> _LoopBoundSession:
        self._client()
        return _LoopBoundSession(self)


class TestOneLoopForEveryCheck:
    def test_settings_and_every_blocking_path_agree(self, tmp_path) -> None:
        """Capability rows run on the gateway loop; the blocking paths must too."""
        acme = _LoopBound()
        _install(stt=(acme,), tts=(acme,))
        clip = tmp_path / "clip.wav"
        clip.write_bytes(b"RIFF")
        cfg = SttConfig(provider="acme")

        async def gateway() -> None:
            (row,) = await speech_providers.registered_entries(
                (acme,), lambda provider: SttConfig()
            )
            assert (row["available"], row["error"]) == (True, False)
            assert await asyncio.to_thread(transcribe.is_available, cfg) is True
            assert await asyncio.to_thread(voice_reply.is_available, "acme") is True
            assert await transcribe.transcribe_audio(str(clip), cfg) == "bound"
            assert acme.loop is asyncio.get_running_loop()

        asyncio.run(gateway())

    def test_a_blocking_caller_first_still_shares_one_loop(self) -> None:
        """With no gateway loop yet, the private loop owns the registry."""
        acme = _LoopBound()
        _install(stt=(acme,))
        cfg = SttConfig(provider="acme")
        assert transcribe.is_available(cfg) is True
        assert transcribe.is_available(cfg) is True

        async def later() -> dict:
            (row,) = await speech_providers.registered_entries(
                (acme,), lambda provider: SttConfig()
            )
            return row

        assert asyncio.run(later())["available"] is True

    def test_a_blocking_check_returns_at_the_timeout(self, monkeypatch) -> None:
        """The wait is bounded even while the provider's own thread is stuck."""
        import threading

        release, finished = threading.Event(), threading.Event()

        def stuck() -> None:
            release.wait(30)  # flake-ok: released right after the call returns
            finished.set()

        class _Stuck(_AcmeStt):
            async def check(self, stt_config):
                await asyncio.to_thread(stuck)
                return ProviderStatus(True)

        monkeypatch.setattr(speech_providers, "CHECK_TIMEOUT_SECS", 0.05)
        try:
            status = speech_providers.check_status_sync(_Stuck(), SttConfig())
            returned_while_stuck = not finished.is_set()
        finally:
            release.set()
        assert returned_while_stuck
        assert (status.available, status.error) == (False, True)

    def test_an_async_check_that_ignores_cancel_still_times_out(self, monkeypatch) -> None:
        """``asyncio.wait`` bounds the caller even when the provider swallows cancel."""
        release_flag: dict = {}

        class _Stubborn(_AcmeStt):
            async def check(self, stt_config):
                release = release_flag["event"]
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue  # ignores the cancel, as a badly written client might
                return ProviderStatus(True)

        monkeypatch.setattr(speech_providers, "CHECK_TIMEOUT_SECS", 0.05)

        async def gateway() -> tuple:
            release_flag["event"] = asyncio.Event()
            (row,) = await speech_providers.registered_entries(
                (_Stubborn(),), lambda provider: SttConfig()
            )
            still_running = not release_flag["event"].is_set()
            release_flag["event"].set()
            await asyncio.sleep(0)
            return row, still_running

        row, still_running = asyncio.run(gateway())
        assert still_running
        assert (row["available"], row["error"]) == (False, True)

    def test_synthesis_after_a_blocking_check_uses_the_same_loop(self) -> None:
        acme = _LoopBound()
        _install(tts=(acme,))
        assert voice_reply.is_available("acme") is True  # the private loop owns it now
        path = asyncio.run(voice_reply.synthesize_speech("hello", provider="acme"))
        assert path is not None
        Path(path).unlink()

    @pytest.mark.asyncio
    async def test_streaming_after_a_blocking_check_uses_the_same_loop(self, monkeypatch) -> None:
        acme = _LoopBoundStreaming()
        _install(stt=(acme,))
        assert await asyncio.to_thread(transcribe.is_available, SttConfig(provider="acme"))
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg(provider="acme")),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _recv_json(ws))["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 8)
            assert await _recv_json(ws) == {"type": "final", "text": "heard 16 bytes"}
            await ws.send_json({"type": "stop"})
            await ws.close()

    @pytest.mark.parametrize("failure", ["factory", "cancelled", "endless"])
    def test_a_cross_loop_synthesis_never_hangs(self, failure, monkeypatch) -> None:
        """Owner on the private loop: a raise, a stray cancel or no end all return."""
        from kiro_crew import voice_reply_registered

        class _Bad(_LoopBound):
            def synthesize(self, text, voice_config):
                if failure == "factory":
                    raise RuntimeError("cannot start")
                return self._stream()

            async def _stream(self):
                self._client()
                if failure == "cancelled":
                    raise asyncio.CancelledError
                while True:
                    yield AudioChunk(b"\x00" * 1024, "audio/mpeg")

        monkeypatch.setattr(voice_reply_registered, "_MAX_REGISTERED_AUDIO_BYTES", 8 * 1024)
        acme = _Bad()
        _install(tts=(acme,))
        assert voice_reply.is_available("acme") is True  # the private loop owns it now
        assert asyncio.run(voice_reply.synthesize_speech("hello", provider="acme")) is None

    def test_a_claimed_private_loop_is_never_replaced(self, monkeypatch) -> None:
        """Even before its thread runs it, the private loop keeps the registry."""
        started = []
        real_start = speech_providers._start_private_loop

        def slow_start() -> asyncio.AbstractEventLoop:
            loop = asyncio.new_event_loop()  # not running yet
            started.append(loop)
            return loop

        monkeypatch.setattr(speech_providers, "_start_private_loop", slow_start)
        private = speech_providers.owner_loop()

        async def gateway() -> asyncio.AbstractEventLoop:
            return speech_providers.owner_loop(asyncio.get_running_loop())

        try:
            assert asyncio.run(gateway()) is private
        finally:
            monkeypatch.setattr(speech_providers, "_start_private_loop", real_start)
            speech_providers._reset_owner_loop()
            for loop in started:
                loop.close()
