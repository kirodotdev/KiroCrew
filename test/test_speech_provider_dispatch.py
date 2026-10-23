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
    yield
    reset_context()
    _invalidate_config_cache()
    sections_stt._WARNED_STT_PROVIDERS.clear()
    sections_stt._DEFERRED_STT_PROVIDERS.clear()
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
#: A built-in row names the provider and nothing else: no status is probed for it.
_BUILTIN_ROW_KEYS = {"name", "builtin"}


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
        assert all(set(row) == _BUILTIN_ROW_KEYS for row in rows if row["builtin"])
        assert all(set(row) == _ROW_KEYS for row in rows if not row["builtin"])
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
        assert "acme" in body["streaming_providers"]

    @pytest.mark.asyncio
    async def test_voice_rows_list_builtins_first_then_acme(self, tmp_path, monkeypatch) -> None:
        acme = _AcmeTts(status=ProviderStatus(False, "Acme needs a sign-in", action=_SIGN_IN))
        _install(tts=(acme,))
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        async with TestClient(TestServer(_voice_app(_make_state(tmp_path)))) as client:
            rows = (await (await client.get("/api/voice/config")).json())["providers"]
        assert rows[:2] == [
            {"name": "system", "builtin": True},
            {"name": "polly", "builtin": True},
        ]
        assert set(rows[-1]) == _ROW_KEYS
        assert rows[-1]["name"] == "acme"
        assert rows[-1]["available"] is False
        assert rows[-1]["reason"] == "Acme needs a sign-in"
        assert rows[-1]["action"] == {"kind": "login", "label": "Sign in to Acme"}
        assert rows[-1]["error"] is False

    @pytest.mark.asyncio
    async def test_opening_settings_runs_no_builtin_probe(self, tmp_path, monkeypatch) -> None:
        """Built-in rows carry no status, so no built-in check runs on a GET."""
        from kiro_crew import voice_reply as vr
        from kiro_crew.dashboard.handlers import core_stt_settings

        def boom(*a, **k):
            raise AssertionError("a built-in provider was probed for its row")

        monkeypatch.setattr(vr, "is_available", boom)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        async with TestClient(TestServer(_voice_app(_make_state(tmp_path)))) as client:
            rows = (await (await client.get("/api/voice/config")).json())["providers"]
        assert all(set(r) == _BUILTIN_ROW_KEYS for r in rows)
        assert core_stt_settings._builtin_stt_rows(["local", "apple", "transcribe", "off"]) == {
            n: {"name": n, "builtin": True} for n in ("local", "apple", "transcribe", "off")
        }

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

    def test_a_registered_provider_gets_no_local_cap_or_split(self) -> None:
        """The local decoder's ceiling and auto-split belong to the built-ins only.

        Every caller skips the duration probe when the cap is None, so a webm
        upload on a host without FFmpeg is not refused as unverifiable before
        it reaches the registered provider.
        """
        _install(stt=(_AcmeStt(),))
        cfg = SttConfig(provider="acme")
        assert transcribe.batch_duration_cap_secs(cfg) is None
        assert transcribe.provider_splits_oversized(cfg) is False

    @pytest.mark.parametrize(
        "provider, cap, splits",
        [("local", True, True), ("apple", True, False), ("transcribe", False, False)],
    )
    def test_the_builtin_caps_are_unchanged(self, provider, cap, splits) -> None:
        cfg = SttConfig(provider=provider)
        expected_cap = transcribe._MAX_AUDIO_SECS if cap else None
        assert transcribe.batch_duration_cap_secs(cfg) == expected_cap
        assert transcribe.provider_splits_oversized(cfg) is splits

    def test_an_unregistered_unknown_name_keeps_the_local_ceiling(self) -> None:
        _install(stt=(_AcmeStt(),))
        cfg = SttConfig(provider="something-new")
        assert transcribe.batch_duration_cap_secs(cfg) == transcribe._MAX_AUDIO_SECS


@pytest.mark.parametrize(
    "lookup, builtin",
    [("stt", name) for name in ("local", "apple", "transcribe", "off")]
    + [("tts", name) for name in ("system", "piper", "polly")],
)
def test_a_builtin_name_never_reaches_the_registry(lookup, builtin) -> None:
    _install(stt=(_AcmeStt(),), tts=(_AcmeTts(),))
    find = speech_providers.stt_provider if lookup == "stt" else speech_providers.tts_provider
    assert find(builtin) is None


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

    @pytest.mark.parametrize("failure", ["format", "factory", "endless"])
    def test_a_bad_stream_answers_none(self, failure, monkeypatch) -> None:
        """An unsupported format, a raise on start, or no end all return ``None``."""
        from kiro_crew import voice_reply_registered

        class _Bad(_AcmeTts):
            def synthesize(self, text, voice_config):
                if failure == "factory":
                    raise RuntimeError("cannot start")
                return self._stream()

            async def _stream(self):
                if failure == "format":
                    yield AudioChunk(b"OggS", "audio/ogg")
                while True:
                    yield AudioChunk(b"\x00" * 1024, "audio/mpeg")

        monkeypatch.setattr(voice_reply_registered, "_MAX_REGISTERED_AUDIO_BYTES", 8 * 1024)
        _install(tts=(_Bad(),))
        assert asyncio.run(voice_reply.synthesize_speech("hi", provider="acme")) is None

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
            language_code="ja-JP",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", vc)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "hello", "slot": "s1"})
            assert resp.status == 200
            assert (await resp.json())["chunks"] == 2
        assert acme.spoken[0][1]["language_code"] == "ja-JP"
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


# ── a check is bounded ───────────────────────────────────────────────────────


class TestCheckIsBounded:
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
        monkeypatch.setattr(speech_providers, "_SYNC_GRACE_SECS", 0.05)
        try:
            status = speech_providers.check_status_sync(_Stuck(), SttConfig())
            returned_while_stuck = not finished.is_set()
        finally:
            release.set()
        assert returned_while_stuck
        assert (status.available, status.error) == (False, True)

    def test_a_blocking_check_that_answers_in_time_keeps_its_answer(self, monkeypatch) -> None:
        """An answer is kept even while the check's own probe thread is still busy."""
        import threading

        release, probe_done = threading.Event(), threading.Event()

        def slow_probe() -> None:
            release.wait(30)  # flake-ok: released right after the call returns
            probe_done.set()

        class _Answers(_AcmeStt):
            async def check(self, stt_config):
                probe = asyncio.ensure_future(asyncio.to_thread(slow_probe))
                await asyncio.sleep(0)
                probe.cancel()
                return ProviderStatus(False, "server unreachable")

        monkeypatch.setattr(speech_providers, "CHECK_TIMEOUT_SECS", 0.5)
        monkeypatch.setattr(speech_providers, "_SYNC_GRACE_SECS", 0.1)
        try:
            status = speech_providers.check_status_sync(_Answers(), SttConfig())
            returned_while_probe_busy = not probe_done.is_set()
        finally:
            release.set()
        # The answer comes back while the check's own probe still holds the
        # loop's executor, so asyncio.run has not returned yet.
        assert returned_while_probe_busy
        assert (status.available, status.reason, status.error) == (
            False,
            "server unreachable",
            False,
        )

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

    def test_a_check_that_cancels_itself_is_a_failure(self) -> None:
        class _SelfCancel(_AcmeStt):
            async def check(self, stt_config):
                raise asyncio.CancelledError

        status = asyncio.run(speech_providers.check_status(_SelfCancel(), SttConfig()))
        assert (status.available, status.error) == (False, True)

    def test_a_cancelled_caller_cancels_the_check(self) -> None:
        seen: dict = {}

        class _Slow(_AcmeStt):
            async def check(self, stt_config):
                seen["started"].set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    seen["cancelled"] = True
                    raise
                return ProviderStatus(True)

        async def caller() -> None:
            seen["started"] = asyncio.Event()
            task = asyncio.create_task(speech_providers.check_status(_Slow(), SttConfig()))
            await await_until(seen["started"], what="the check to start")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)

        asyncio.run(caller())
        assert seen.get("cancelled") is True

    @pytest.mark.parametrize("failure", ["raises", "not_async"])
    def test_a_failed_blocking_check_reports_an_error(self, failure) -> None:
        class _Broken(_AcmeStt):
            if failure == "raises":

                async def check(self, stt_config):
                    raise RuntimeError("down")

            else:

                def check(self, stt_config):  # type: ignore[override]
                    return ProviderStatus(True)

        status = speech_providers.check_status_sync(_Broken(), SttConfig())
        assert (status.available, status.error) == (False, True)


# ── the Text-to-Speech language ──────────────────────────────────────────────


class TestTtsLanguage:
    """``voice_reply.language_code``: validated, saved, served, handed to a provider."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", ""),
            ("   ", ""),
            ("en-US", "en-US"),
            (" zh-CN ", "zh-CN"),
            ("yue", "yue"),
            ("zh-Hant-TW", "zh-Hant-TW"),
        ],
    )
    def test_a_language_tag_or_empty_is_accepted(self, value, expected) -> None:
        assert voice_reply.validated_language_code(value) == expected

    @pytest.mark.parametrize(
        "value", [None, 5, ["en-US"], {}, "english", "en_US", "e", "en-", "en-US; rm -rf"]
    )
    def test_anything_else_is_rejected(self, value) -> None:
        assert voice_reply.validated_language_code(value) is None

    def test_synthesis_settings_carry_it_and_drop_a_malformed_one(self) -> None:
        def language(section):
            return voice_reply.synthesis_settings(section)["language_code"]

        assert language(None) == ""
        assert language({"language_code": "de-DE"}) == "de-DE"
        assert language({"language_code": "not a tag"}) == ""

    def test_the_loader_reads_it_and_drops_a_malformed_one(self, monkeypatch) -> None:
        from kiro_crew.slack.handler import _vc, load_voice_reply_config

        monkeypatch.setattr(_vc, "language_code", "")
        monkeypatch.setattr(_vc, "provider", _vc.provider)
        monkeypatch.setattr(_vc, "system_voice", _vc.system_voice)
        _write_config({"voice_reply": {"language_code": "ko-KR"}})
        load_voice_reply_config()
        assert _vc.language_code == "ko-KR"
        _write_config({"voice_reply": {"language_code": ["ko-KR"]}})
        load_voice_reply_config()
        assert _vc.language_code == ""

    def test_the_provider_receives_it(self) -> None:
        acme = _AcmeTts()
        _install(tts=(acme,))
        path = asyncio.run(
            voice_reply.synthesize_speech("hi", provider="acme", language_code="fr-FR")
        )
        assert path is not None
        Path(path).unlink()
        assert acme.spoken[0][1]["language_code"] == "fr-FR"

    @pytest.mark.asyncio
    async def test_the_put_saves_it_and_the_get_serves_it(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.slack.handler import _vc

        _install(tts=(_AcmeTts(),))
        path = _write_config({"voice_reply": {"provider": "acme"}})
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.config_path", lambda: path)
        monkeypatch.setattr(_vc, "provider", "acme")
        monkeypatch.setattr(_vc, "language_code", "")
        async with TestClient(TestServer(_voice_app(_make_state(tmp_path)))) as client:
            before = await (await client.get("/api/voice/config")).json()
            assert before["language_code"] == ""
            assert before["language_codes"][0] == "" and "ja-JP" in before["language_codes"]

            resp = await client.put("/api/voice/config", json={"language_code": "ja-JP"})
            assert resp.status == 200
            assert json.loads(path.read_text())["voice_reply"]["language_code"] == "ja-JP"
            assert _vc.language_code == "ja-JP"
            after = await (await client.get("/api/voice/config")).json()
            assert after["language_code"] == "ja-JP"

            resp = await client.put("/api/voice/config", json={"language_code": "Japanese!"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "voice_language_invalid"
        assert json.loads(path.read_text())["voice_reply"]["language_code"] == "ja-JP"
        assert _vc.language_code == "ja-JP"

    @pytest.mark.asyncio
    async def test_a_stored_code_outside_the_list_stays_selectable(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.slack.handler import _vc

        monkeypatch.setattr(_vc, "language_code", "th-TH")
        async with TestClient(TestServer(_voice_app(_make_state(tmp_path)))) as client:
            body = await (await client.get("/api/voice/config")).json()
        assert body["language_codes"][-1] == "th-TH"
        assert body["language_codes"].count("th-TH") == 1
