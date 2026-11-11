"""The ``openai_compatible`` speech provider: endpoint rules, audio plumbing, live and file paths.

The server side is a loopback aiohttp app that speaks just enough of the OpenAI
Realtime transcription socket and ``/audio/transcriptions``, so the provider is
exercised end to end without a network or a key.
"""

from __future__ import annotations

import array
import asyncio
import base64
import math
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import transcribe
from kiro_crew.stt import openai_compat as oc

SPEECH = array.array("h", [int(9000 * math.sin(i / 6.0)) for i in range(1600)]).tobytes()
QUIET = bytes(3200)  # 100 ms of digital silence at 16 kHz


def _stt(**over):
    base = dict(openai_model="gpt-4o-transcribe", language_code="auto", silence_ms=300)
    base.update(over)
    return SimpleNamespace(**base)


# ── model id ──


@pytest.mark.parametrize(
    "value,expected",
    [
        ("gpt-4o-transcribe", "gpt-4o-transcribe"),
        ("  whisper-1 ", "whisper-1"),
        ("Systran/faster-whisper-small", "Systran/faster-whisper-small"),
        ("", None),
        ("   ", None),
        ("bad model", None),
        ("x" * 129, None),
        ("-leading", None),
        (None, None),
        (3, None),
    ],
)
def test_model_id_rule(value, expected):
    assert oc.openai_model_name(value) == expected


# ── base URL ──


def test_base_url_accepts_https_and_private_network_http():
    assert oc.validate_base_url("https://stt.example.com/v1/") == "https://stt.example.com/v1"
    assert oc.validate_base_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1"
    # A server on the owner's own network: loopback, RFC 1918 and link-local literals.
    for url in ("http://10.0.0.5:8910/v1", "http://192.168.1.5/v1", "http://172.16.0.9/v1"):
        assert oc.validate_base_url(url) == url


@pytest.mark.parametrize(
    "url", ["http://8.8.8.8/v1", "http://[2606:4700:4700::1111]/v1", "http://0x7f.1/v1"]
)
def test_cleartext_http_to_a_public_literal_is_refused(url):
    with pytest.raises(oc.EndpointError):
        oc.validate_base_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://stt.example.com/v1",  # cleartext off this machine
        "http://localhost:8000/v1",  # a name is not proof of loopback
        "https://user:pw@stt.example.com/v1",
        "https://stt.example.com/v1?key=1",
        "https://stt.example.com/v1#x",
        "ftp://stt.example.com",
        "not a url",
        "https://",
    ],
)
def test_base_url_refusals_never_echo_the_value(url):
    with pytest.raises(oc.EndpointError) as caught:
        oc.validate_base_url(url)
    assert caught.value.code == oc.CODE_NOT_CONFIGURED
    assert url not in str(caught.value) or url == "not a url"


# ── vault ──


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    from kiro_crew.config.paths import config_dir
    from kiro_crew.secrets import SecretVault

    return SecretVault(config_dir())


def test_nothing_stored_is_not_configured(vault):
    with pytest.raises(oc.EndpointError) as caught:
        oc.resolve_endpoint()
    assert caught.value.code == oc.CODE_NOT_CONFIGURED
    ready = oc.availability()
    assert (ready.ok, ready.code) == (False, oc.CODE_NOT_CONFIGURED)


def test_a_key_alone_means_openai_itself(vault):
    vault.set_sync(oc.VAULT_API_KEY, "sk-test")
    endpoint = oc.resolve_endpoint()
    assert (endpoint.base_url, endpoint.api_key, endpoint.default_url) == (
        oc.DEFAULT_BASE_URL,
        "sk-test",
        True,
    )
    assert oc.availability().ok


def test_a_loopback_server_needs_no_key(vault):
    vault.set_sync(oc.VAULT_BASE_URL, "http://127.0.0.1:9/v1")
    endpoint = oc.resolve_endpoint()
    assert (endpoint.base_url, endpoint.api_key) == ("http://127.0.0.1:9/v1", "")


def test_a_key_that_is_not_one_line_is_refused(vault):
    vault.set_sync(oc.VAULT_API_KEY, "sk-test\r\nX-Evil: 1")
    with pytest.raises(oc.EndpointError):
        oc.resolve_endpoint()


def test_config_json_cannot_redirect_the_audio():
    """The endpoint is read from the vault only; no config field names a URL or a key."""
    from dataclasses import fields

    from kiro_crew.config.sections import SttConfig

    names = {f.name for f in fields(SttConfig)}
    assert not {n for n in names if "url" in n or "api_key" in n or n == "endpoint"}


# ── audio ──


def _samples(pcm: bytes) -> list[int]:
    out = array.array("h")
    out.frombytes(pcm)
    return out.tolist()


def test_resampler_is_chunk_invariant_and_scales_the_rate():
    ramp = array.array("h", [i * 10 for i in range(1600)]).tobytes()
    whole = oc._Resampler().process(ramp)
    split = oc._Resampler()
    pieces = b"".join(split.process(ramp[i : i + 322]) for i in range(0, len(ramp), 322))
    assert pieces == whole
    assert abs(len(_samples(whole)) - 1600 * 3 // 2) <= 2
    out = _samples(whole)
    assert all(a <= b for a, b in zip(out, out[1:]))


def test_turn_gate_commits_after_speech_then_silence():
    gate = oc._TurnGate(silence_ms=300)
    verdicts = [gate.push(SPEECH) for _ in range(3)]
    assert verdicts == ["", "", ""]
    verdicts = [gate.push(QUIET) for _ in range(3)]
    assert verdicts == ["", "", "commit"]


def test_turn_gate_discards_audio_that_never_held_speech():
    gate = oc._TurnGate(silence_ms=300)
    seen = [gate.push(QUIET) for _ in range(60)]
    assert "commit" not in seen
    assert "discard" in seen


def test_turn_gate_cuts_a_turn_that_never_pauses():
    gate = oc._TurnGate(silence_ms=700)
    seen = [gate.push(SPEECH) for _ in range(oc.MAX_TURN_MS // 100 + 2)]
    assert "commit" in seen


# ── fake server ──


class FakeOpenAI:
    """Just enough of the Realtime transcription socket and the file endpoint."""

    def __init__(self, *, reject_with=None, error_on_commit=False, no_realtime=False):
        self.reject_with = reject_with
        self.error_on_commit = error_on_commit
        self.no_realtime = no_realtime
        self.uploads: list[dict] = []
        self.auth: list[str | None] = []
        self.updates: list[dict] = []
        self.audio = bytearray()
        self.commits = 0
        self.clears = 0
        self.upload: dict | None = None
        self.app = web.Application()
        self.app.router.add_get("/v1/realtime", self.realtime)
        self.app.router.add_post("/v1/audio/transcriptions", self.transcriptions)
        self.app.router.add_get("/v1/models", self.models)

    async def models(self, request: web.Request) -> web.Response:
        self.auth.append(request.headers.get("Authorization"))
        if self.reject_with:
            return web.Response(status=self.reject_with)
        return web.json_response({"object": "list", "data": [{"id": "m"}]})

    async def realtime(self, request: web.Request) -> web.StreamResponse:
        self.auth.append(request.headers.get("Authorization"))
        assert request.query.get("intent") == "transcription"
        if self.no_realtime:
            return web.Response(status=404)
        if self.reject_with:
            return web.Response(status=self.reject_with)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "session.created", "session": {}})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            event = msg.json()
            kind = event["type"]
            if kind == "session.update":
                self.updates.append(event)
            elif kind == "input_audio_buffer.append":
                self.audio += base64.b64decode(event["audio"])
            elif kind == "input_audio_buffer.clear":
                self.clears += 1
                self.audio.clear()
            elif kind == "input_audio_buffer.commit":
                self.commits += 1
                if self.error_on_commit:
                    await ws.send_json(
                        {"type": "error", "error": {"code": "boom", "message": "SECRET-DETAIL"}}
                    )
                    continue
                item = f"item_{self.commits}"
                await ws.send_json({"type": "input_audio_buffer.committed", "item_id": item})
                await ws.send_json(
                    {
                        "type": "conversation.item.input_audio_transcription.delta",
                        "item_id": item,
                        "delta": "hello ",
                    }
                )
                await ws.send_json(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": item,
                        "transcript": f"hello {self.commits}",
                    }
                )
        return ws

    async def transcriptions(self, request: web.Request) -> web.Response:
        self.auth.append(request.headers.get("Authorization"))
        if self.reject_with:
            return web.Response(status=self.reject_with)
        form = await request.post()
        upload = form["file"]
        self.upload = {
            "model": form["model"],
            "language": form.get("language"),
            "filename": upload.filename,
            "size": len(upload.file.read()),
        }
        self.uploads.append(self.upload)
        return web.json_response({"text": f" spoken words {len(self.uploads)} "})


@pytest_asyncio.fixture
async def fake(monkeypatch):
    async def make(**kwargs):
        server = FakeOpenAI(**kwargs)
        test_server = TestServer(server.app, host="127.0.0.1")
        await test_server.start_server()
        endpoint = oc.Endpoint(f"http://127.0.0.1:{test_server.port}/v1", "sk-test", False)
        monkeypatch.setattr(oc, "resolve_endpoint", lambda: endpoint)
        servers.append(test_server)
        return server

    servers: list[TestServer] = []
    yield make
    for test_server in servers:
        await test_server.close()


async def _collect(session) -> list[dict]:
    return [event async for event in session.events()]


# ── live session ──


@pytest.mark.asyncio
async def test_live_turn_streams_partial_then_final_in_order(fake):
    server = await fake()
    session = oc.StreamingSession(_stt(language_code="vi-VN"))
    assert await session.start() is None
    collector = asyncio.create_task(_collect(session))
    for chunk in [SPEECH] * 3 + [QUIET] * 3:
        assert await session.feed(chunk)
    await session.finish()
    events = await asyncio.wait_for(collector, 5)
    await session.close()

    assert events == [
        {"type": "partial", "text": "hello "},
        {"type": "final", "text": "hello 1"},
    ]
    assert server.auth == ["Bearer sk-test"]
    update = server.updates[0]["session"]
    assert update["type"] == "transcription"
    assert update["audio"]["input"]["turn_detection"] is None
    assert update["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert update["audio"]["input"]["transcription"] == {
        "model": "gpt-4o-transcribe",
        "language": "vi",
    }
    # 600 ms of 16 kHz audio reaches the server as 24 kHz.
    assert abs(len(server.audio) // 2 - 600 * 24) <= 4


@pytest.mark.asyncio
async def test_live_model_that_takes_a_language_list(fake):
    server = await fake()
    session = oc.StreamingSession(_stt(openai_model="gpt-live-transcribe", language_code="en-US"))
    assert await session.start() is None
    await session.close()
    transcription = server.updates[0]["session"]["audio"]["input"]["transcription"]
    assert transcription == {"model": "gpt-live-transcribe", "languages": ["en"]}


@pytest.mark.asyncio
async def test_auto_language_sends_none(fake):
    server = await fake()
    session = oc.StreamingSession(_stt(language_code="auto"))
    assert await session.start() is None
    await session.close()
    assert "language" not in server.updates[0]["session"]["audio"]["input"]["transcription"]


@pytest.mark.asyncio
async def test_finish_commits_the_turn_still_in_flight(fake):
    server = await fake()
    session = oc.StreamingSession(_stt())
    assert await session.start() is None
    collector = asyncio.create_task(_collect(session))
    for chunk in [SPEECH] * 2:
        assert await session.feed(chunk)
    await session.finish()
    events = await asyncio.wait_for(collector, 5)
    await session.close()
    assert [e["type"] for e in events] == ["partial", "final"]
    assert server.commits == 1


@pytest.mark.asyncio
async def test_silence_is_cleared_not_transcribed(fake):
    server = await fake()
    session = oc.StreamingSession(_stt())
    assert await session.start() is None
    collector = asyncio.create_task(_collect(session))
    for _ in range(60):
        assert await session.feed(QUIET)
    await session.finish()
    assert await asyncio.wait_for(collector, 5) == []
    await session.close()
    assert server.commits == 0
    assert server.clears >= 1


@pytest.mark.asyncio
async def test_rejected_key_is_reported_with_its_own_code(fake):
    await fake(reject_with=401)
    session = oc.StreamingSession(_stt())
    message = await session.start()
    assert message == "The speech server refused the API key."
    assert session.failure_code == oc.CODE_AUTH
    await session.close()


@pytest.mark.asyncio
async def test_server_error_text_is_not_relayed(fake):
    await fake(error_on_commit=True)
    session = oc.StreamingSession(_stt())
    assert await session.start() is None
    collector = asyncio.create_task(_collect(session))
    for chunk in [SPEECH] * 3 + [QUIET] * 3:
        await session.feed(chunk)
    events = await asyncio.wait_for(collector, 5)
    await session.close()
    assert events == [
        {"type": "error", "message": "The speech server reported an error.", "code": oc.CODE_FAILED}
    ]
    assert "SECRET-DETAIL" not in repr(events)


@pytest.mark.asyncio
async def test_nothing_configured_refuses_to_start(vault):
    session = oc.StreamingSession(_stt())
    message = await session.start()
    assert message and "Settings > Secrets" in message
    assert session.failure_code == oc.CODE_NOT_CONFIGURED
    await session.close()


# ── files ──


@pytest.mark.asyncio
async def test_file_upload_names_the_container_and_trims_the_text(fake, tmp_path):
    server = await fake()
    wav = tmp_path / "memo"
    wav.write_bytes(b"RIFF" + bytes(60))
    text = await oc.transcribe_file(str(wav), _stt(language_code="de-DE"))
    assert text == "spoken words 1"
    assert server.upload == {
        "model": "gpt-4o-transcribe",
        "language": "de",
        "filename": "audio.wav",
        "size": 64,
    }
    assert server.auth == ["Bearer sk-test"]


@pytest.mark.asyncio
async def test_file_failures_return_none_and_empty_file_is_silence(fake, tmp_path):
    await fake(reject_with=500)
    clip = tmp_path / "clip.webm"
    clip.write_bytes(b"\x1a\x45\xdf\xa3" + bytes(10))
    assert await oc.transcribe_file(str(clip), _stt()) is None
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert await oc.transcribe_file(str(empty), _stt()) == ""
    assert await oc.transcribe_file(str(tmp_path / "missing"), _stt()) is None


def test_container_sniffing():
    assert oc.sniff_extension(b"OggS....") == ".ogg"
    assert oc.sniff_extension(b"\x00\x00\x00\x18ftypM4A ") == ".m4a"
    assert oc.sniff_extension(b"\x1a\x45\xdf\xa3....") == ".webm"
    assert oc.sniff_extension(b"???") == ".webm"


# ── wiring ──


def test_provider_name_is_pinned_across_modules():
    from kiro_crew.config.sections import SttConfig, valid_stt_providers

    assert transcribe.STT_PROVIDER_OPENAI == oc.PROVIDER == "openai_compatible"
    assert oc.PROVIDER in valid_stt_providers()
    assert valid_stt_providers()[-1] == "off"
    assert SttConfig().openai_model == oc.OPENAI_MODEL_DEFAULT


def test_auto_language_is_kept_for_this_provider_only_with_local():
    from kiro_crew.config.sections import SttConfig

    assert (
        SttConfig(provider="openai_compatible", language_code="auto").effective_language_code
        == "auto"
    )
    assert SttConfig(provider="local", language_code="auto").effective_language_code == "auto"
    assert SttConfig(provider="transcribe", language_code="auto").effective_language_code == "en-US"


def test_a_bad_stored_model_degrades_to_the_default(caplog):
    from kiro_crew.config.sections import _validated_stt_openai_model

    assert _validated_stt_openai_model(None) == oc.OPENAI_MODEL_DEFAULT
    assert _validated_stt_openai_model(" whisper-1 ") == "whisper-1"
    with caplog.at_level("WARNING"):
        assert _validated_stt_openai_model("has space") == oc.OPENAI_MODEL_DEFAULT
    assert "has space" not in caplog.text


def test_this_provider_streams_but_has_no_duration_ceiling_or_split(vault):
    from kiro_crew.dashboard.stt_stream import streaming_providers

    assert oc.PROVIDER in streaming_providers()
    config = SimpleNamespace(provider=oc.PROVIDER)
    assert transcribe.batch_duration_cap_secs(config) is None
    assert transcribe.provider_splits_oversized(config) is False


@pytest.mark.asyncio
async def test_batch_dispatch_goes_to_this_provider(fake, tmp_path):
    server = await fake()
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF" + bytes(30))
    config = SimpleNamespace(
        enabled=True,
        provider=oc.PROVIDER,
        openai_model="whisper-1",
        language_code="auto",
        timeout_secs=30,  # flake-ok: the upload's HTTP ceiling in config, never waited on
    )
    assert await transcribe.transcribe_audio(str(clip), config) == "spoken words 1"
    assert server.upload["model"] == "whisper-1"


@pytest.mark.asyncio
async def test_dictation_socket_carries_the_provider_through_core_redaction_and_audit(
    fake, monkeypatch
):
    """The real ``/api/ws/stt``: gate, dispatch, caps, redaction and the audit pair all apply."""
    from stt_stream_helpers import _cfg, _make_app, _recv_json, _wait_for_operation

    from kiro_crew.dashboard import stt_stream

    server = await fake()
    audit: list[dict] = []
    monkeypatch.setattr(
        stt_stream.KiroCrewConfig,
        "load",
        classmethod(lambda cls: _cfg(provider=oc.PROVIDER, silence_ms=300)),
    )
    monkeypatch.setattr(stt_stream, "check_origin", lambda request, require: True)
    monkeypatch.setattr(
        stt_stream,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **event: audit.append(event)),
    )
    monkeypatch.setattr(stt_stream, "_build_endpointer", lambda *args: None)
    monkeypatch.setattr(stt_stream, "_redacted", lambda text: text.replace("hello", "howdy"))
    async with TestClient(TestServer(_make_app(), host="127.0.0.1")) as client:
        ws = await client.ws_connect("/api/ws/stt")
        ready = await _recv_json(ws)
        assert ready["type"] == "ready"
        # The generic cap applies: Meetings reads this figure to rotate its socket.
        assert ready["max_duration_ms"] > 0
        for chunk in [SPEECH] * 3 + [QUIET] * 3:
            await ws.send_bytes(chunk)
        assert await _recv_json(ws) == {"type": "partial", "text": "howdy"}
        assert await _recv_json(ws) == {"type": "final", "text": "howdy 1"}
        await ws.send_json({"type": "stop"})
        await ws.close()
    await _wait_for_operation(audit, "stt_stream_end")
    assert [event["operation"] for event in audit] == ["stt_stream_start", "stt_stream_end"]
    assert server.auth == ["Bearer sk-test"]


@pytest.mark.asyncio
async def test_dictation_socket_reports_a_refused_key_by_code(fake, monkeypatch):
    from stt_stream_helpers import _cfg, _make_app, _recv_json, _wait_for_operation

    from kiro_crew.dashboard import stt_stream

    await fake(reject_with=403)
    audit: list[dict] = []
    monkeypatch.setattr(
        stt_stream.KiroCrewConfig, "load", classmethod(lambda cls: _cfg(provider=oc.PROVIDER))
    )
    monkeypatch.setattr(stt_stream, "check_origin", lambda request, require: True)
    monkeypatch.setattr(
        stt_stream,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **event: audit.append(event)),
    )
    monkeypatch.setattr(stt_stream, "_build_endpointer", lambda *args: None)
    async with TestClient(TestServer(_make_app(), host="127.0.0.1")) as client:
        ws = await client.ws_connect("/api/ws/stt")
        frame = await _recv_json(ws)
        assert frame["type"] == "error"
        assert frame["code"] == oc.CODE_AUTH
        await ws.close()
    await _wait_for_operation(audit, "stt_stream_end")
    assert [event["operation"] for event in audit] == ["stt_stream_start", "stt_stream_end"]


# ── a server with no Realtime socket ──


@pytest.mark.asyncio
async def test_server_without_realtime_is_driven_turn_by_turn(fake):
    server = await fake(no_realtime=True)
    session = oc.StreamingSession(_stt(language_code="vi-VN"))
    assert await session.start() is None
    collector = asyncio.create_task(_collect(session))
    for chunk in [SPEECH] * 3 + [QUIET] * 3 + [SPEECH] * 2:
        assert await session.feed(chunk)
    await session.finish()
    events = await asyncio.wait_for(collector, 5)
    await session.close()

    # One final per turn, in the order spoken, and no partials: the server answers a turn whole.
    assert events == [
        {"type": "final", "text": "spoken words 1"},
        {"type": "final", "text": "spoken words 2"},
    ]
    assert [u["filename"] for u in server.uploads] == ["audio.wav", "audio.wav"]
    assert server.uploads[0]["language"] == "vi"
    assert server.uploads[0]["model"] == "gpt-4o-transcribe"
    assert set(server.auth) == {"Bearer sk-test"}


@pytest.mark.asyncio
async def test_turn_upload_is_a_16khz_mono_wav_of_exactly_the_turn(fake, monkeypatch):
    await fake(no_realtime=True)
    sent: list[bytes] = []
    real = oc._post_transcription

    async def spy(http, endpoint, data, model, language):
        sent.append(data)
        return await real(http, endpoint, data, model, language)

    monkeypatch.setattr(oc, "_post_transcription", spy)
    session = oc.StreamingSession(_stt())
    assert await session.start() is None
    collector = asyncio.create_task(_collect(session))
    for chunk in [SPEECH] * 3 + [QUIET] * 3:
        await session.feed(chunk)
    await session.finish()
    await asyncio.wait_for(collector, 5)
    await session.close()

    import io
    import wave

    with wave.open(io.BytesIO(sent[0])) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        assert w.getnframes() == 1600 * 3 + 1600 * 3  # the speech plus the pause that ended it


@pytest.mark.asyncio
async def test_wrong_key_on_a_server_without_realtime_is_a_setup_failure(fake):
    await fake(reject_with=401)
    session = oc.StreamingSession(_stt())
    assert await session.start() == "The speech server refused the API key."
    assert session.failure_code == oc.CODE_AUTH
    await session.close()


@pytest.mark.asyncio
async def test_silence_only_uploads_nothing_in_turn_mode(fake):
    server = await fake(no_realtime=True)
    session = oc.StreamingSession(_stt())
    assert await session.start() is None
    collector = asyncio.create_task(_collect(session))
    for _ in range(60):
        assert await session.feed(QUIET)
    await session.finish()
    assert await asyncio.wait_for(collector, 5) == []
    await session.close()
    assert server.uploads == []


@pytest.mark.asyncio
async def test_unreachable_server_is_not_mistaken_for_a_missing_realtime_route(monkeypatch):
    endpoint = oc.Endpoint("http://127.0.0.1:9/v1", "sk-test", False)  # nothing listens on 9
    monkeypatch.setattr(oc, "resolve_endpoint", lambda: endpoint)
    session = oc.StreamingSession(_stt())
    assert await session.start() == "Could not connect to the speech server."
    assert session.failure_code == oc.CODE_FAILED
    await session.close()
