"""Codex voice contract tests: no Codex installation, account, microphone or network."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import transcribe
from kiro_crew.config.sections_stt import SttConfig, _validated_stt_provider
from kiro_crew.stt import codex, codex_media, codex_rpc


def _fake_sandbox(monkeypatch, launch, directory=None, configured="auto"):
    """Replace the sandbox boundary with a recorder; returns (wrap, launcher file)."""
    launcher = None
    if directory is not None:
        launcher = directory / "launcher"
        launcher.write_text("", encoding="utf-8")

    wrap = AsyncMock(
        side_effect=lambda argv, **options: (
            ["sandbox-launcher", *argv],
            dict(options["env"]),
            None if launcher is None else str(launcher),
        )
    )
    monkeypatch.setattr(
        codex_rpc,
        "sandbox",
        SimpleNamespace(
            sandboxed_spawn_argv_async=wrap,
            create_subprocess_limited=launch,
            configured_sandbox_mode=lambda: configured,
        ),
    )
    return wrap, launcher


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured,expected",
    [("auto", "standard"), ("off", "standard"), ("cc", "cc"), ("strict", "strict")],
)
async def test_child_sandbox_is_never_looser_than_standard(monkeypatch, configured, expected):
    proc = SimpleNamespace(
        stdout=SimpleNamespace(readline=AsyncMock(side_effect=asyncio.CancelledError))
    )
    wrap, _ = _fake_sandbox(monkeypatch, AsyncMock(return_value=proc), configured=configured)
    monkeypatch.setattr(codex_rpc.platform_compat, "kill_and_reap", AsyncMock())
    rpc = codex_rpc.CodexRpc()
    rpc.call = AsyncMock(return_value={"userAgent": "unknown"})
    with pytest.raises(codex_rpc.CodexError):
        await rpc.start("/installed/codex")
    assert wrap.await_args.kwargs["mode"] == expected
    assert wrap.await_args.kwargs["require_confinement"] is True
    assert "is_kiro_cli" not in wrap.await_args.kwargs


@pytest.fixture
def wire():
    return json.loads(
        (Path(__file__).parent / "fixtures" / "codex_voice.json").read_text(encoding="utf-8")
    )


def test_codex_is_opt_in_and_preserves_default():
    assert SttConfig().provider == "local"
    assert _validated_stt_provider("codex") == "codex"
    config = SttConfig(provider="codex")
    assert transcribe.batch_duration_cap_secs(config) == codex.MAX_AUDIO_SECS
    assert not transcribe.provider_splits_oversized(config)


@pytest.mark.parametrize(
    "binary,extra,code",
    [
        (None, None, codex.CODE_MISSING),
        ("/installed/codex", None, codex.CODE_EXTRA),
        ("/installed/codex", object(), ""),
    ],
)
def test_local_prerequisites_never_contact_codex(monkeypatch, binary, extra, code):
    monkeypatch.setattr(codex, "codex_executable", lambda: binary)
    monkeypatch.setattr(codex.importlib.util, "find_spec", lambda name: extra)
    monkeypatch.setattr(codex.sandbox, "no_backend_refuses_spawn", lambda mode, **options: False)
    verdict = codex.availability()
    assert verdict.code == code
    assert verdict.ok is (code == "")


def _refuses(mode):
    raise OSError("probe failed")


@pytest.mark.parametrize(
    "probe,code",
    [
        (lambda mode: True, codex.CODE_SANDBOX),
        (_refuses, codex.CODE_SANDBOX),
        (lambda mode: False, ""),
    ],
)
def test_availability_reports_a_host_the_sandbox_would_refuse(monkeypatch, probe, code):
    seen = []
    monkeypatch.setattr(codex, "codex_executable", lambda: "/installed/codex")
    monkeypatch.setattr(codex.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(codex.codex_rpc, "sandbox_mode", lambda: "strict")

    def check(mode):
        seen.append(mode)
        return probe(mode)

    monkeypatch.setattr(codex.sandbox, "no_backend_refuses_spawn", check)
    verdict = codex.availability()
    assert (verdict.ok, verdict.code) == (code == "", code)
    assert seen == ["strict"]


@pytest.mark.parametrize(
    "backend,floor,mode,inside,refused",
    [
        ("namespace", None, "standard", False, False),
        ("none", None, "standard", False, True),
        ("none", "strict", "standard", False, True),
        ("none", None, "off", False, True),
        ("none", None, "standard", True, False),
    ],
)
def test_the_settings_hint_reports_when_confinement_is_unavailable(
    monkeypatch,
    backend,
    floor,
    mode,
    inside,
    refused,
):
    # Patched through the facade: these names have several holders, and only the
    # facade's setattr reaches all of them (test_facade_mirror_guards).
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: floor)
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: inside)
    monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: None)
    monkeypatch.setattr(sandbox, "detect_backend", lambda config_mode="auto": backend)
    assert sandbox.no_backend_refuses_spawn(mode) is refused


@pytest.mark.asyncio
async def test_only_input_asr_is_emitted_exactly_once(wire):
    session = codex.StreamingSession()
    for event in wire["data"]:
        session._data_event(event)
    session._data_event(wire["data"][1])
    session.finished = True
    session._emit(None)
    events = [event async for event in session.events()]
    assert events == [
        {"type": "partial", "text": "Kiro"},
        {"type": "partial", "text": "Kiro Crew"},
        {"type": "partial", "text": "Kiro Crew works"},
        {"type": "final", "text": "Kiro Crew works"},
    ]


def test_untrusted_codex_candidates_are_skipped(monkeypatch):
    planted, installed = "/home/user/.local/bin/codex", "/opt/codex/bin/codex"

    def validate(candidate):
        if candidate == planted:
            raise ValueError("inside the agent workspace")
        return candidate

    monkeypatch.setattr(codex.github_runner, "PROVIDER_EXECUTABLE_DIRS", ())
    monkeypatch.setattr(codex.os.path, "isfile", lambda path: path in {planted, installed})
    monkeypatch.setattr(
        codex.github_runner, "provider_executable_candidates", lambda name: (planted, installed)
    )
    monkeypatch.setattr(codex.github_runner, "validate_provider_executable", validate)
    assert codex.codex_executable() == installed

    monkeypatch.setattr(
        codex.github_runner, "provider_executable_candidates", lambda name: (planted,)
    )
    assert codex.codex_executable() is None
    assert codex.availability().code == codex.CODE_MISSING


def test_oversized_transcript_item_id_is_rejected():
    session = codex.StreamingSession()
    long_id = "x" * (codex._MAX_ITEM_ID_CHARS + 1)
    with pytest.raises(ValueError):
        session._data_event({"type": "input_transcript.added", "item": {"id": long_id, "text": ""}})
    assert session.seen == set()


def test_well_known_install_wins_over_an_earlier_path_entry(monkeypatch):
    # Built with os.path.join, as the resolver builds them, so Windows
    # separators match.
    system = codex.os.path.join("/usr/local/bin", "codex")
    planted = codex.os.path.join("/home/user/.local/bin", "codex")
    monkeypatch.setattr(codex.github_runner, "PROVIDER_EXECUTABLE_DIRS", ("/usr/local/bin",))
    monkeypatch.setattr(codex.os.path, "isfile", lambda path: path in {system, planted})
    monkeypatch.setattr(
        codex.github_runner, "provider_executable_candidates", lambda name: (planted, system)
    )
    monkeypatch.setattr(codex.github_runner, "validate_provider_executable", lambda path: path)
    assert codex.codex_executable() == system


@pytest.mark.asyncio
async def test_fragment_spaces_and_repeated_words_are_not_normalized():
    session = codex.StreamingSession()
    for ident, text in enumerate(["我", "们", " say", " very", " very", " well"]):
        session._data_event(
            {"type": "input_transcript.added", "item": {"id": str(ident), "text": text}}
        )
    assert session.pending_text == "我们 say very very well"


@pytest.mark.asyncio
async def test_audio_bounds_and_pacing(monkeypatch):
    sleeps = []

    async def tick(delay):
        sleeps.append(delay)

    monkeypatch.setattr(codex_media, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": tick}))
    source = codex_media.PcmInput()
    source.append(b"\x01\x00" * codex_media.FRAME_SAMPLES)
    data, pts = await source.read()
    assert data == b"\x01\x00" * codex_media.FRAME_SAMPLES
    assert pts == 0
    data, pts = await source.read()
    assert data == b"\0" * (codex_media.FRAME_SAMPLES * 2)
    assert pts == codex_media.FRAME_SAMPLES
    assert len(sleeps) == 2
    await source.drain()
    with pytest.raises(ValueError, match="whole samples"):
        source.append(b"x")
    with pytest.raises(ValueError, match="backlog"):
        source.append(b"\0" * (codex_media.MAX_BUFFER_BYTES + 2))


@pytest.mark.asyncio
async def test_feed_failure_is_loud_and_close_is_idempotent(monkeypatch):
    session = codex.StreamingSession()
    session.track = codex_media.PcmInput()
    session.track.stop = lambda: None
    assert not await session.feed(b"x")
    event = await anext(session.events())
    assert event["type"] == "error"
    await session.close()
    await session.close()


@pytest.mark.asyncio
async def test_media_cleanup_failure_still_reaps_owned_process():
    session = codex.StreamingSession()

    def failed_stop():
        raise RuntimeError("track already failed")

    session.track = SimpleNamespace(stop=failed_stop)
    session.pc = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("peer failed")))
    session.rpc.close = AsyncMock()
    await session.close()
    session.rpc.close.assert_awaited_once()
    assert session.track is None
    assert session.pc is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["client_request", "close_requested"])
async def test_finish_closes_session_and_flushes_late_asr_without_a_turn(monkeypatch, wire, reason):
    session = codex.StreamingSession()
    session.track = SimpleNamespace(drain=AsyncMock())
    session._data_event({"type": "input_transcript.added", "item": {"id": "x", "text": "tail"}})

    async def tail(_delay):
        pass

    sent = []

    def send(raw):
        sent.append(json.loads(raw))
        session._data_event(
            {"type": "input_transcript.added", "item": {"id": "late", "text": " end"}}
        )
        session._data_event({**wire["closed"], "reason": reason})

    session.channel = SimpleNamespace(send=send)

    monkeypatch.setattr(codex, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": tail}))
    await session.finish()
    await session.finish()
    events = [event async for event in session.events()]
    assert [e for e in events if e["type"] == "final"] == [{"type": "final", "text": "tail end"}]
    assert sent == [{"type": "session.close"}]
    assert session.confirmed
    assert not await session.feed(b"\0\0")


@pytest.mark.asyncio
async def test_no_final_text_or_silence_success_without_session_closed(monkeypatch):
    session = codex.StreamingSession()
    session.track = SimpleNamespace(drain=AsyncMock())
    session.channel = SimpleNamespace(send=lambda raw: None)

    async def tick(_delay):
        pass

    async def timed_out(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(
        codex, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": tick, "wait_for": timed_out})
    )
    await session.finish()
    events = [event async for event in session.events()]
    assert [event["type"] for event in events] == ["error"]
    assert not session.confirmed


@pytest.mark.asyncio
async def test_unrequested_server_finalization_is_an_error(wire):
    session = codex.StreamingSession()
    session._data_event(wire["closed"])
    assert session.failure is not None
    assert not session.confirmed


@pytest.mark.asyncio
async def test_failed_finalization_preserves_partial_as_failure(monkeypatch):
    session = codex.StreamingSession()
    session.track = SimpleNamespace(drain=AsyncMock(side_effect=TimeoutError))
    session.pending_text = "unfinished"
    await session.finish()
    events = [event async for event in session.events()]
    assert [event["type"] for event in events] == ["error"]
    assert session.pending_text == "unfinished"


class FakePeer:
    def __init__(self):
        self.handlers = {}
        self.channel = SimpleNamespace(on=self.channel_on)
        self.localDescription = SimpleNamespace(sdp="fixture-offer")
        self.connectionState = "new"
        self.closed = False

    def on(self, name):
        def register(callback):
            self.handlers[name] = callback
            return callback

        return register

    def channel_on(self, name):
        return self.on("channel:" + name)

    def createDataChannel(self, name):
        assert name == "oai-events"
        return self.channel

    async def createOffer(self):
        return "fixture-offer"

    async def setLocalDescription(self, value):
        assert value == "fixture-offer"

    async def setRemoteDescription(self, value):
        assert value == "fixture-answer"
        self.connectionState = "connected"
        self.handlers["connectionstatechange"]()
        self.handlers["channel:message"](json.dumps({"type": "session.started"}))

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_start_uses_client_managed_handoffs_and_no_agent_turns(monkeypatch, wire):
    peer = FakePeer()
    track = SimpleNamespace(stop=lambda: None)
    monkeypatch.setattr(codex, "availability", lambda: codex.Availability(True))
    monkeypatch.setattr(codex, "codex_executable", lambda: "/installed/codex")
    monkeypatch.setattr(codex_media, "create_peer", lambda: (peer, track))
    monkeypatch.setattr(codex_media, "remote_answer", lambda value: value)
    session = codex.StreamingSession()
    session.rpc.start = AsyncMock()
    session.rpc.close = AsyncMock()
    session.rpc.directory = SimpleNamespace(name="/voice-fixture-directory")
    calls = []

    async def call(method, params):
        calls.append((method, params))
        if method == "thread/start":
            return wire["thread"]
        if method == "thread/realtime/start":
            session.rpc.events.put_nowait(wire["sdp"])
        return {}

    session.rpc.call = call
    try:
        assert await session.start() is None
        params = next(params for method, params in calls if method == "thread/realtime/start")
        assert params["clientManagedHandoffs"] is True
        assert params["flushTranscriptTailOnSessionEnd"] is False
        assert params["includeStartupContext"] is False
        assert params["version"] == "v3"
        assert params["transport"] == {"type": "webrtc", "sdp": "fixture-offer"}
        assert [name for name, _ in calls] == ["thread/start", "thread/realtime/start"]
    finally:
        await session.close()
    assert peer.closed
    session.rpc.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_rpc_response_timeout_during_start_is_a_retryable_failure(monkeypatch):
    rpc = codex_rpc.CodexRpc()
    rpc.process = SimpleNamespace(returncode=None)
    rpc.write = AsyncMock()
    monkeypatch.setattr(codex_rpc, "RPC_TIMEOUT_SECS", 0)

    with pytest.raises(codex_rpc.CodexError) as raised:
        await rpc.call("thread/start", {})

    assert raised.value.code == codex_rpc.CODE_FAILED
    assert str(raised.value) == "Codex did not answer the dictation request."
    assert not rpc.pending


@pytest.mark.asyncio
async def test_readiness_timeout_during_start_is_a_retryable_failure(monkeypatch, wire):
    peer = FakePeer()
    track = SimpleNamespace(stop=lambda: None)
    monkeypatch.setattr(codex, "availability", lambda: codex.Availability(True))
    monkeypatch.setattr(codex, "codex_executable", lambda: "/installed/codex")
    monkeypatch.setattr(codex_media, "create_peer", lambda: (peer, track))
    monkeypatch.setattr(codex, "_CONNECT_SECS", 0)
    session = codex.StreamingSession()
    session.rpc.start = AsyncMock()
    session.rpc.close = AsyncMock()
    session.rpc.directory = SimpleNamespace(name="/voice-fixture-directory")
    session.rpc.call = AsyncMock(
        side_effect=[wire["thread"], {}],
    )

    assert await session.start() == "The Codex dictation connection timed out."
    assert session.failure.code == codex.CODE_FAILED
    assert peer.closed
    session.rpc.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_initialized_write_during_start_is_a_retryable_failure(monkeypatch, wire):
    incoming = asyncio.Queue()

    def write(data):
        message = json.loads(data)
        if message.get("method") == "initialize":
            incoming.put_nowait(
                json.dumps({"id": message["id"], "result": wire["initialize"]}).encode()
            )
        elif message.get("method") == "initialized":
            raise BrokenPipeError

    proc = SimpleNamespace(
        returncode=None,
        stdin=SimpleNamespace(write=write, drain=AsyncMock()),
        stdout=SimpleNamespace(readline=incoming.get),
    )
    from kiro_crew import sandbox

    monkeypatch.setattr(
        sandbox,
        "sandboxed_spawn_argv_async",
        AsyncMock(return_value=(["sandbox-launcher"], {}, None)),
    )
    monkeypatch.setattr(sandbox, "create_subprocess_limited", AsyncMock(return_value=proc))
    monkeypatch.setattr(codex_rpc.platform_compat, "kill_and_reap", AsyncMock())
    monkeypatch.setattr(codex, "availability", lambda: codex.Availability(True))
    monkeypatch.setattr(codex, "codex_executable", lambda: "/installed/codex")
    peer = FakePeer()
    track = SimpleNamespace(stop=lambda: None)
    monkeypatch.setattr(codex_media, "create_peer", lambda: (peer, track))

    session = codex.StreamingSession()
    assert await session.start() == "The Codex dictation connection failed."
    assert session.failure.code == codex.CODE_FAILED
    assert peer.closed


@pytest.mark.asyncio
async def test_unsupported_voice_start_closes_owned_resources(monkeypatch):
    session = codex.StreamingSession()
    monkeypatch.setattr(
        codex, "availability", lambda: codex.Availability(False, codex.CODE_MISSING, "missing")
    )
    session.rpc.close = AsyncMock()
    assert await session.start() == "missing"
    assert session.failure.code == codex.CODE_MISSING
    session.rpc.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["turn/start", "thread/realtime/appendText", "thread/realtime/appendSpeech", "command/exec"],
)
async def test_rpc_cannot_dispatch_tasks(method):
    rpc = codex_rpc.CodexRpc()
    with pytest.raises(ValueError, match="agent turns"):
        await rpc.call(method, {})


@pytest.mark.asyncio
async def test_reader_denies_approvals_and_wakes_pending_calls_on_exit():
    frames = [
        json.dumps(
            {"id": "approval-1", "method": "item/commandExecution/requestApproval"}
        ).encode(),
        b"",
    ]
    rpc = codex_rpc.CodexRpc()
    rpc.process = SimpleNamespace(stdout=SimpleNamespace(readline=AsyncMock(side_effect=frames)))
    rpc.write = AsyncMock()
    future = asyncio.get_running_loop().create_future()
    rpc.pending[1] = future
    await rpc._read()
    assert rpc.write.await_args.args[0]["error"]["code"] == -32601
    assert future.done()
    with pytest.raises(codex_rpc.CodexError):
        await future
    assert rpc.events.get_nowait()["method"] == codex_rpc.TRANSPORT_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frames",
    [[b""], [b"not json\n"], [b"[1, 2]\n"]],
    ids=["eof", "malformed", "non-object"],
)
async def test_transport_failure_mid_dictation_is_a_retryable_failure(frames):
    session = codex.StreamingSession()
    session.rpc.process = SimpleNamespace(
        stdout=SimpleNamespace(readline=AsyncMock(side_effect=frames))
    )
    await session.rpc._read()
    await asyncio.wait_for(session._rpc_events(), 5)
    assert session.failure.code == codex.CODE_FAILED
    event = session.queue.get_nowait()
    assert event["type"] == "error"
    assert event["code"] == codex.CODE_FAILED


@pytest.mark.asyncio
async def test_codex_realtime_error_still_reports_voice_unavailable():
    session = codex.StreamingSession()
    session.rpc.process = SimpleNamespace(
        stdout=SimpleNamespace(
            readline=AsyncMock(
                side_effect=[
                    json.dumps({"method": "thread/realtime/error", "params": {}}).encode(),
                    b"",
                ]
            )
        )
    )
    await session.rpc._read()
    await asyncio.wait_for(session._rpc_events(), 5)
    assert session.failure.code == codex.CODE_UNAVAILABLE
    assert session.queue.get_nowait()["code"] == codex.CODE_UNAVAILABLE
    assert session.queue.empty()


@pytest.mark.asyncio
async def test_codex_cannot_forge_the_transport_failure_event():
    rpc = codex_rpc.CodexRpc()
    rpc.process = SimpleNamespace(
        stdout=SimpleNamespace(
            readline=AsyncMock(
                side_effect=[
                    json.dumps({"method": codex_rpc.TRANSPORT_FAILED, "params": {}}).encode(),
                    b"",
                ]
            )
        )
    )
    await rpc._read()
    assert rpc.events.get_nowait()["method"] == codex_rpc.TRANSPORT_FAILED
    assert rpc.events.empty()


@pytest.mark.asyncio
async def test_failed_write_during_start_is_a_retryable_failure():
    rpc = codex_rpc.CodexRpc()
    rpc.process = SimpleNamespace(
        returncode=None,
        stdin=SimpleNamespace(
            write=lambda data: None, drain=AsyncMock(side_effect=BrokenPipeError)
        ),
    )
    with pytest.raises(codex_rpc.CodexError) as raised:
        await rpc.call("thread/start", {})
    assert raised.value.code == codex_rpc.CODE_FAILED
    assert not rpc.pending


@pytest.mark.asyncio
async def test_batch_dispatch_keeps_shared_redaction_and_path_guard(monkeypatch):
    config = SttConfig(provider="codex")
    monkeypatch.setattr(transcribe, "_is_sensitive_audio_path", lambda path: False)
    monkeypatch.setattr(codex, "transcribe_file", AsyncMock(return_value="private"))
    monkeypatch.setattr(transcribe, "_redact_transcript", lambda text: "safe")
    assert await transcribe.transcribe_audio("fixture.wav", config) == "safe"
    codex.transcribe_file.assert_awaited_once_with("fixture.wav")
    monkeypatch.setattr(transcribe, "_is_sensitive_audio_path", lambda path: True)
    codex.transcribe_file.reset_mock()
    assert await transcribe.transcribe_audio("sensitive.wav", config) is None
    codex.transcribe_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("account_type", ["chatgpt", "apiKey"])
async def test_rpc_login_env_and_process_cleanup(monkeypatch, wire, account_type, tmp_path):
    incoming = asyncio.Queue()
    messages = []

    def write(data):
        frame = json.loads(data)
        messages.append(frame)
        if "id" not in frame:
            return
        result = (
            wire["initialize"]
            if frame["method"] == "initialize"
            else {"account": {"type": account_type}}
        )
        incoming.put_nowait(json.dumps({"id": frame["id"], "result": result}).encode())

    proc = SimpleNamespace(
        returncode=None,
        stdin=SimpleNamespace(write=write, drain=AsyncMock()),
        stdout=SimpleNamespace(readline=incoming.get),
    )
    launch = AsyncMock(return_value=proc)
    reap = AsyncMock()
    wrap, launcher = _fake_sandbox(monkeypatch, launch, tmp_path)
    monkeypatch.setattr(codex_rpc.platform_compat, "kill_and_reap", reap)
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "fixture-secret-never-forward")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/fixture/bus")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/fixture/runtime")
    rpc = codex_rpc.CodexRpc()
    try:
        if account_type == "chatgpt":
            await rpc.start("/installed/codex")
        else:
            with pytest.raises(codex_rpc.CodexError) as error:
                await rpc.start("/installed/codex")
            assert error.value.code == codex_rpc.CODE_LOGIN
        wrapped = wrap.await_args.args[0]
        assert wrapped[:4] == ["/installed/codex", "app-server", "--listen", "stdio://"]
        assert "mcp_servers={}" in wrapped
        assert wrap.await_args.kwargs["mode"] == "standard"
        sandbox_env = wrap.await_args.kwargs["env"]
        assert all("fixture-secret" not in value for value in sandbox_env.values())
        assert "DBUS_SESSION_BUS_ADDRESS" not in sandbox_env
        assert "XDG_RUNTIME_DIR" not in sandbox_env
        argv = launch.await_args.args
        assert argv[0] == "sandbox-launcher"
        assert argv[1:] == tuple(wrapped)
        directory = Path(launch.await_args.kwargs["cwd"])
        assert [m["method"] for m in messages] == ["initialize", "initialized", "account/read"]
    finally:
        await rpc.close()
    reap.assert_awaited_once_with(proc)
    assert not directory.exists()
    assert not launcher.exists()
    assert rpc.reader is None


@pytest.mark.asyncio
async def test_rpc_refuses_to_start_without_the_sandbox(monkeypatch):
    launch = AsyncMock()

    async def refuse(argv, **options):
        raise RuntimeError("no sandbox backend")

    monkeypatch.setattr(
        codex_rpc,
        "sandbox",
        SimpleNamespace(
            sandboxed_spawn_argv_async=refuse,
            create_subprocess_limited=launch,
            configured_sandbox_mode=lambda: "auto",
        ),
    )
    rpc = codex_rpc.CodexRpc()
    with pytest.raises(codex_rpc.CodexError) as error:
        await rpc.start("/installed/codex")
    assert error.value.code == codex_rpc.CODE_SANDBOX
    launch.assert_not_awaited()
    assert rpc.directory is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "one two"])
async def test_recorded_audio_uses_same_session_and_cleans_up(monkeypatch, text):
    import numpy as np

    from kiro_crew import transcribe_local

    async def events():
        if text:
            yield {"type": "final", "text": text}

    fake = SimpleNamespace(
        start=AsyncMock(return_value=None),
        feed=AsyncMock(return_value=True),
        finish=AsyncMock(),
        close=AsyncMock(),
        events=events,
    )
    monkeypatch.setattr(codex, "StreamingSession", lambda: fake)
    monkeypatch.setattr(
        transcribe_local, "_pcm_from_wav", lambda *args: np.zeros(1600, dtype=np.float32)
    )
    monkeypatch.setattr(
        codex, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": AsyncMock()})
    )
    assert await codex.transcribe_file("fixture.wav") == text
    fake.feed.assert_awaited_once()
    fake.finish.assert_awaited_once()
    fake.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_rejects_oversized_audio_without_opening_codex(monkeypatch):
    import numpy as np

    from kiro_crew import transcribe_local

    start = AsyncMock()
    monkeypatch.setattr(codex.StreamingSession, "start", start)
    monkeypatch.setattr(
        transcribe_local,
        "_pcm_from_wav",
        lambda *args: np.zeros(
            codex.MAX_AUDIO_SECS * codex_media.SAMPLE_RATE + 1, dtype=np.float32
        ),
    )
    assert await codex.transcribe_file("long.wav") is None
    start.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent",
    ["kiro_crew_dictation/0.162.0 (fixture)", "unknown", "kiro_crew_dictation/0.163.0-alpha"],
)
async def test_old_or_unknown_codex_cannot_ignore_handoff_controls(monkeypatch, agent):
    rpc = codex_rpc.CodexRpc()
    proc = SimpleNamespace(
        stdout=SimpleNamespace(readline=AsyncMock(side_effect=asyncio.CancelledError))
    )
    _fake_sandbox(monkeypatch, AsyncMock(return_value=proc))
    monkeypatch.setattr(codex_rpc.platform_compat, "kill_and_reap", AsyncMock())
    rpc.call = AsyncMock(return_value={"userAgent": agent})
    rpc.write = AsyncMock()
    with pytest.raises(codex_rpc.CodexError) as error:
        await rpc.start("/installed/codex")
    assert error.value.code == codex_rpc.CODE_UNAVAILABLE
    assert rpc.call.await_count == 1
    assert rpc.process is None
