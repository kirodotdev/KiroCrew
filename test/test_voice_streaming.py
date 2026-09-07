"""Local speech must arrive before process exit and stop with its requesting client."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import sys
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import voice_reply as voice
from kiro_crew.dashboard import chat_voice


class RawPiper:
    def __init__(self):
        self.stdin = MagicMock(drain=AsyncMock())
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self.killed = False
        self.reaped = False

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def communicate(self):
        self.reaped = True
        return b"", b""


@pytest.fixture
def piper_env(tmp_path, monkeypatch):
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"model")
    config = tmp_path / "voice.onnx.json"
    config.write_text(json.dumps({"audio": {"sample_rate": 22050}}), encoding="utf-8")
    cleanup = tmp_path / "sandbox-launcher"
    cleanup.write_text("test", encoding="utf-8")
    monkeypatch.setattr(voice, "_resolve_piper_binary", lambda _binary: "piper")
    wrap = AsyncMock(side_effect=lambda argv, **kw: (argv, str(cleanup)))
    monkeypatch.setattr(voice, "wrap_argv_async", wrap)
    monkeypatch.setattr(voice, "cgroup_scope_argv", lambda argv: argv)
    return model, config, cleanup, wrap


@pytest.mark.asyncio
async def test_first_pcm_precedes_eof_and_early_close_reaps(piper_env, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-only-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-only-token")
    model, _config, cleanup, wrap = piper_env
    proc = RawPiper()
    proc.stdout.feed_data(b"\x01\x00\x02\x00")
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(voice, "create_subprocess_limited", spawn)
    stream = voice.streaming_piper_reply("你好。下一句话！", piper_model=str(model))
    async with contextlib.aclosing(stream):
        idx, sample_rate, pcm = await asyncio.wait_for(anext(stream), timeout=2)
        assert (idx, sample_rate, pcm) == (0, 22050, b"\x01\x00\x02\x00")
        assert proc.returncode is None
        assert not proc.reaped
        assert "--output-raw" in spawn.call_args.args
        assert "-f" not in spawn.call_args.args
        child_env = spawn.call_args.kwargs["env"]
        assert "AWS_SECRET_ACCESS_KEY" not in child_env
        assert "SLACK_BOT_TOKEN" not in child_env
        assert child_env["PYTHONIOENCODING"] == "utf-8"
        assert wrap.call_args.kwargs["mode"] == "standard"
    assert proc.killed and proc.reaped
    assert not cleanup.exists()
    assert spawn.call_count == 1


@pytest.mark.asyncio
async def test_pcm_odd_pipe_reads_preserve_samples(piper_env, monkeypatch):
    model, _config, _cleanup, _wrap = piper_env
    proc = RawPiper()
    proc.stdout.feed_data(b"\x01\x02\x03")
    monkeypatch.setattr(voice, "create_subprocess_limited", AsyncMock(return_value=proc))
    stream = voice.streaming_piper_reply("one. two.", piper_model=str(model))
    async with contextlib.aclosing(stream):
        first = await asyncio.wait_for(anext(stream), timeout=2)
        proc.stdout.feed_data(b"\x04\x05\x06")
        proc.stdout.feed_eof()
        proc.stderr.feed_eof()
        rest = [chunk async for chunk in stream]
    pcm = first[2] + b"".join(chunk[2] for chunk in rest)
    assert pcm == b"\x01\x02\x03\x04\x05\x06"
    assert proc.stdin.write.call_args_list[0].args[0] == b"one.\n"
    assert proc.stdin.write.call_args_list[1].args[0] == b"two.\n"
    with wave.open(io.BytesIO(voice.pcm_to_wav(pcm, 22050))) as audio:
        assert audio.getframerate() == 22050
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.readframes(audio.getnframes()) == pcm
    assert not proc.killed and proc.reaped


@pytest.mark.asyncio
@pytest.mark.parametrize("sample_rate", [None, "22050", True, 0, 999999])
async def test_invalid_model_format_never_spawns(piper_env, monkeypatch, sample_rate):
    model, config, _cleanup, _wrap = piper_env
    config.write_text(json.dumps({"audio": {"sample_rate": sample_rate}}), encoding="utf-8")
    spawn = AsyncMock()
    monkeypatch.setattr(voice, "create_subprocess_limited", spawn)
    with pytest.raises(voice.VoiceSynthesisError) as error:
        async for _ in voice.streaming_piper_reply("hello", piper_model=str(model)):
            pytest.fail("invalid format emitted audio")
    assert error.value.code == "voice_model_config_invalid"
    spawn.assert_not_awaited()


@pytest.mark.parametrize("target", ["model", "config"])
def test_protected_model_paths_are_refused_before_read(piper_env, monkeypatch, target):
    model, config, _cleanup, _wrap = piper_env
    # Keep the real credential anchor; stub the reader instead of creating or
    # reading any file in the operator's credential directory.
    protected = Path.home() / ".aws" / "credentials"
    values = {"model": str(model), "config": str(config)}
    values[target] = str(protected)
    reader = MagicMock(side_effect=AssertionError("protected path was opened"))
    monkeypatch.setattr(voice, "open", reader, raising=False)
    with pytest.raises(voice.VoiceSynthesisError) as error:
        voice._piper_model_settings(**values)
    assert error.value.code == "voice_model_path_forbidden"
    reader.assert_not_called()


@pytest.mark.parametrize("target", ["model", "config"])
def test_unresolved_model_paths_are_refused_before_read(piper_env, monkeypatch, target):
    model, config, _cleanup, _wrap = piper_env
    values = {"model": str(model), "config": str(config)}

    def check(path):
        if path == values[target]:
            raise voice.PathResolutionStalled(path, str(model.parent))
        return False

    reader = MagicMock(side_effect=AssertionError("unverified path was opened"))
    monkeypatch.setattr(voice, "is_sensitive_path", check)
    monkeypatch.setattr(voice, "open", reader, raising=False)
    with pytest.raises(voice.VoiceSynthesisError) as error:
        voice._piper_model_settings(**values)
    assert error.value.code == "voice_model_path_forbidden"
    reader.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "partial_sample", "exit", "limit"])
async def test_stream_failures_are_coded_and_reaped(piper_env, monkeypatch, failure):
    model, _config, cleanup, _wrap = piper_env
    proc = RawPiper()
    monkeypatch.setattr(voice, "create_subprocess_limited", AsyncMock(return_value=proc))
    codes = {
        "empty": "voice_invalid_audio",
        "partial_sample": "voice_invalid_audio",
        "exit": "voice_synthesis_failed",
        "limit": "voice_audio_limit",
    }
    if failure == "partial_sample":
        proc.stdout.feed_data(b"\x01")
    elif failure == "limit":
        monkeypatch.setattr(voice, "_PIPER_MAX_AUDIO_BYTES", 2)
        proc.stdout.feed_data(b"\x01\x00\x02\x00")
    elif failure == "exit":
        proc.returncode = 1
    proc.stdout.feed_eof()
    proc.stderr.feed_eof()
    with pytest.raises(voice.VoiceSynthesisError) as error:
        async for _ in voice.streaming_piper_reply("hello", piper_model=str(model)):
            pass
    assert error.value.code == codes[failure]
    assert proc.reaped
    assert not cleanup.exists()


class FailedResident:
    """A resident attempt that retires before handing its failure to the caller."""

    def __init__(self, failure, *, partial=False):
        self.failure = failure
        self.partial = partial
        self.retired = False

    async def stream(self, *_args, **_kwargs):
        try:
            if self.partial:
                yield 0, 22050, b"\x01\x00"
            raise self.failure
        finally:
            self.retired = True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["voice_invalid_audio", "voice_synthesis_failed", "spawn"])
async def test_resident_failure_before_pcm_falls_back_after_retirement(
    piper_env, monkeypatch, failure
):
    model, _config, cleanup, wrap = piper_env
    monkeypatch.setattr("kiro_crew.piper_runtime.python_piper_available", lambda: True)
    if failure == "spawn":
        error = OSError("worker could not start")
    else:
        error = voice.VoiceSynthesisError(failure, "worker could not run")
    resident = FailedResident(error)
    proc = RawPiper()
    proc.stdout.feed_data(b"\x02\x00\x03\x00")
    proc.stdout.feed_eof()
    proc.stderr.feed_eof()

    async def spawn(*_args, **_kwargs):
        assert resident.retired, "CLI started while the resident attempt still owned its worker"
        return proc

    monkeypatch.setattr(voice, "create_subprocess_limited", spawn)
    stream = voice.streaming_piper_reply("hello", piper_model=str(model), runtime=resident)
    async with contextlib.aclosing(stream):
        chunks = [frame async for frame in stream]
    assert chunks == [(0, 22050, b"\x02\x00\x03\x00")]
    assert proc.reaped and not cleanup.exists()
    assert wrap.call_args.kwargs["mode"] == "standard"
    assert "first_party_fixed_argv" not in wrap.call_args.kwargs


@pytest.mark.asyncio
async def test_probe_true_but_isolated_worker_exits_before_ready_uses_cli(piper_env, monkeypatch):
    from kiro_crew import piper_runtime

    model, _config, _cleanup, _wrap = piper_env
    monkeypatch.setattr(piper_runtime, "python_piper_available", lambda: True)
    monkeypatch.setattr(
        piper_runtime, "wrap_argv_async", AsyncMock(side_effect=lambda argv, **_kw: (argv, None))
    )
    monkeypatch.setattr(piper_runtime, "cgroup_scope_argv", lambda argv: argv)
    worker = RawPiper()
    worker.returncode = 1
    worker.stdout.feed_eof()
    worker.stderr.feed_data(b"No module named piper")
    worker.stderr.feed_eof()
    worker_spawn = AsyncMock(return_value=worker)
    monkeypatch.setattr(piper_runtime, "create_subprocess_limited", worker_spawn)
    cli = RawPiper()
    cli.stdout.feed_data(b"\x02\x00")
    cli.stdout.feed_eof()
    cli.stderr.feed_eof()

    async def spawn(*_args, **_kwargs):
        assert worker.reaped
        return cli

    monkeypatch.setattr(voice, "create_subprocess_limited", spawn)
    runtime = piper_runtime.PiperRuntime()
    try:
        stream = voice.streaming_piper_reply("hello", piper_model=str(model), runtime=runtime)
        async with contextlib.aclosing(stream):
            assert [frame async for frame in stream] == [(0, 22050, b"\x02\x00")]
        assert worker_spawn.await_count == 1
        assert "-E" in worker_spawn.call_args.args and "-P" in worker_spawn.call_args.args
        assert runtime._proc is None and cli.reaped
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["voice_sandbox_unavailable", "voice_cancelled", "voice_timeout", "voice_invalid_request"],
)
async def test_resident_policy_cancel_and_deadline_failures_never_try_cli(
    piper_env, monkeypatch, failure
):
    model, _config, _cleanup, wrap = piper_env
    monkeypatch.setattr("kiro_crew.piper_runtime.python_piper_available", lambda: True)
    resident = FailedResident(voice.VoiceSynthesisError(failure, "attempt refused"))
    spawn = AsyncMock()
    monkeypatch.setattr(voice, "create_subprocess_limited", spawn)
    stream = voice.streaming_piper_reply("hello", piper_model=str(model), runtime=resident)
    async with contextlib.aclosing(stream):
        with pytest.raises(voice.VoiceSynthesisError) as error:
            await anext(stream)
    assert error.value.code == failure
    assert resident.retired
    spawn.assert_not_awaited()
    wrap.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.CancelledError, PermissionError])
async def test_resident_interruption_or_os_refusal_never_tries_cli(piper_env, monkeypatch, failure):
    model, _config, _cleanup, wrap = piper_env
    monkeypatch.setattr("kiro_crew.piper_runtime.python_piper_available", lambda: True)
    resident = FailedResident(failure())
    stream = voice.streaming_piper_reply("hello", piper_model=str(model), runtime=resident)
    async with contextlib.aclosing(stream):
        with pytest.raises(failure):
            await anext(stream)
    assert resident.retired
    wrap.assert_not_awaited()


@pytest.mark.asyncio
async def test_resident_failure_after_pcm_never_replays_the_text(piper_env, monkeypatch):
    model, _config, _cleanup, wrap = piper_env
    monkeypatch.setattr("kiro_crew.piper_runtime.python_piper_available", lambda: True)
    resident = FailedResident(
        voice.VoiceSynthesisError("voice_invalid_audio", "worker lost"), partial=True
    )
    stream = voice.streaming_piper_reply("hello", piper_model=str(model), runtime=resident)
    async with contextlib.aclosing(stream):
        assert await anext(stream) == (0, 22050, b"\x01\x00")
        with pytest.raises(voice.VoiceSynthesisError) as error:
            await anext(stream)
    assert error.value.code == "voice_invalid_audio"
    wrap.assert_not_awaited()


@pytest.mark.asyncio
async def test_resident_failure_without_a_cli_keeps_its_original_error(piper_env, monkeypatch):
    model, _config, _cleanup, wrap = piper_env
    monkeypatch.setattr("kiro_crew.piper_runtime.python_piper_available", lambda: True)
    monkeypatch.setattr(voice, "_resolve_piper_binary", lambda _binary: None)
    original = voice.VoiceSynthesisError("voice_invalid_audio", "worker could not import")
    resident = FailedResident(original)
    stream = voice.streaming_piper_reply("hello", piper_model=str(model), runtime=resident)
    async with contextlib.aclosing(stream):
        with pytest.raises(voice.VoiceSynthesisError) as error:
            await anext(stream)
    assert error.value is original
    wrap.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_resident", [False, True])
async def test_original_deadline_cancels_and_reaps_cli_including_a_fallback(
    piper_env, monkeypatch, use_resident
):
    model, _config, cleanup, _wrap = piper_env
    monkeypatch.setattr("kiro_crew.piper_runtime.python_piper_available", lambda: True)
    resident = FailedResident(voice.VoiceSynthesisError("voice_invalid_audio", "worker lost"))
    proc = RawPiper()
    spawned = asyncio.Event()
    deadline = asyncio.timeout(None)
    deadlines = []

    def shared_deadline(seconds):
        deadlines.append(seconds)
        return deadline

    async def spawn(*_args, **_kwargs):
        assert not use_resident or resident.retired
        spawned.set()
        return proc

    monkeypatch.setattr(voice.asyncio, "timeout", shared_deadline)
    monkeypatch.setattr(voice, "create_subprocess_limited", spawn)
    stream = voice.streaming_piper_reply(
        "hello", piper_model=str(model), runtime=resident if use_resident else None
    )
    task = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        # Expire the original owner after CLI starts. A fresh fallback deadline
        # cannot replace it, and teardown still has to reap the active process.
        deadline.reschedule(asyncio.get_running_loop().time())
        with pytest.raises(voice.VoiceSynthesisError) as error:
            await asyncio.wait_for(task, 5)
        assert error.value.code == "voice_timeout"
        assert deadlines == [voice._PIPER_STREAM_TIMEOUT_SECONDS]
        assert proc.killed and proc.reaped and not cleanup.exists()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("composition_failed", [False, True])
async def test_cli_diagnostic_uses_composed_log_redaction(
    piper_env, monkeypatch, caplog, composition_failed
):
    from dataclasses import replace

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import (
        PlatformCompositionError,
        build_default_context,
        reset_context,
        set_context,
    )
    from kiro_crew.platform.context import LOG_WITHHELD_PLACEHOLDER
    from kiro_crew.platform.defaults import DefaultCredentialPolicy

    class Policy(DefaultCredentialPolicy):
        def redact(self, text):
            if composition_failed:
                raise PlatformCompositionError("test policy could not be composed")
            return super().redact(text).replace("SSO-COOKIE", "[REDACTED-SSO]")

    model, _config, cleanup, _wrap = piper_env
    proc = RawPiper()
    proc.returncode = 1
    proc.stdout.feed_eof()
    proc.stderr.feed_data(b"native failure: SSO-COOKIE AKIAIOSFODNN7EXAMPLE")
    proc.stderr.feed_eof()
    monkeypatch.setattr(voice, "create_subprocess_limited", AsyncMock(return_value=proc))
    set_context(replace(build_default_context(KiroCrewConfig()), credentials=Policy()))
    try:
        with pytest.raises(voice.VoiceSynthesisError) as error:
            async for _ in voice.streaming_piper_reply("hello", piper_model=str(model)):
                pytest.fail("failed synthesis emitted audio")
        assert error.value.code == "voice_synthesis_failed"
        assert "Piper stream failed" in caplog.text
        expected = LOG_WITHHELD_PLACEHOLDER if composition_failed else "[REDACTED-SSO]"
        assert expected in caplog.text
        assert "SSO-COOKIE" not in caplog.text
        assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text
        assert proc.reaped and not cleanup.exists()
    finally:
        reset_context()


@pytest.mark.asyncio
async def test_sandbox_refusal_stays_closed(piper_env, monkeypatch):
    model, _config, _cleanup, _wrap = piper_env
    refusal = voice.SandboxUnavailableError("sandbox backend unavailable", "no_backend", "test")
    monkeypatch.setattr(voice, "wrap_argv_async", AsyncMock(side_effect=refusal))
    spawn = AsyncMock()
    monkeypatch.setattr(voice, "create_subprocess_limited", spawn)
    with pytest.raises(voice.VoiceSynthesisError) as error:
        async for _ in voice.streaming_piper_reply("hello", piper_model=str(model)):
            pytest.fail("a refused provider emitted audio")
    assert error.value.code == "voice_sandbox_unavailable"
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_first_pcm_reaps(piper_env, monkeypatch):
    model, _config, cleanup, _wrap = piper_env
    proc = RawPiper()
    spawned = asyncio.Event()

    async def spawn(*_args, **_kwargs):
        spawned.set()
        return proc

    monkeypatch.setattr(voice, "create_subprocess_limited", spawn)
    stream = voice.streaming_piper_reply("hello", piper_model=str(model))
    pending = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(spawned.wait(), timeout=2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=2)
        assert proc.killed and proc.reaped
        assert not cleanup.exists()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_repeated_cancel_joins_cli_reap_and_removes_sandbox(piper_env, monkeypatch):
    model, _config, cleanup, _wrap = piper_env
    proc = RawPiper()
    spawned = asyncio.Event()
    reaping = asyncio.Event()
    release_reap = asyncio.Event()

    async def spawn(*_args, **_kwargs):
        spawned.set()
        return proc

    async def communicate():
        reaping.set()
        await release_reap.wait()
        proc.reaped = True
        return b"", b""

    proc.communicate = communicate
    monkeypatch.setattr(voice, "create_subprocess_limited", spawn)
    stream = voice.streaming_piper_reply("hello", piper_model=str(model))
    task = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(spawned.wait(), 2)
        task.cancel()
        await asyncio.wait_for(reaping.wait(), 2)
        task.cancel()
        # Deliver the repeated cancellation while the reap remains blocked.
        await asyncio.sleep(0)
        assert not task.done()
        release_reap.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert proc.killed and proc.reaped and not cleanup.exists()
    finally:
        release_reap.set()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        await stream.aclose()


@pytest.mark.asyncio
async def test_real_pipe_can_be_played_then_cancelled_without_waiting_for_exit(
    piper_env, monkeypatch, tmp_path
):
    model, _config, cleanup, _wrap = piper_env
    processes = []

    async def spawn(*_args, **kwargs):
        # This child has no Piper dependency; it exercises real OS pipe delivery
        # and reap on every platform. Its output and CWD belong to this test.
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys, threading; sys.stdout.buffer.write(bytes(8)); "
            "sys.stdout.buffer.flush(); threading.Event().wait(60)",
            cwd=tmp_path,
            **kwargs,
        )
        processes.append(proc)
        return proc

    monkeypatch.setattr(voice, "create_subprocess_limited", spawn)
    stream = voice.streaming_piper_reply("hello", piper_model=str(model))
    try:
        async with contextlib.aclosing(stream):
            _idx, _rate, pcm = await asyncio.wait_for(anext(stream), timeout=5)
            assert pcm == bytes(8)
            assert processes[0].returncode is None
        assert processes[0].returncode is not None
        assert not cleanup.exists()
    finally:
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
            await asyncio.wait_for(proc.communicate(), timeout=5)


def test_chinese_and_long_unpunctuated_text_keep_every_character():
    assert voice.split_sentences("你好。怎么样？很好！\n接着说") == [
        "你好。",
        "怎么样？",
        "很好！",
        "接着说",
    ]
    text = "没有标点的长中文" * 100
    phrases = voice._piper_phrases(text)
    assert "".join(phrases) == text
    assert len(phrases) > 1
    assert all(len(phrase) <= voice._PIPER_MAX_PHRASE_CHARS for phrase in phrases)


def test_phrase_redaction_is_applied_after_markdown_stripping():
    phrases = voice._piper_phrases("Key: AKIAIOSFOD**NN7EXAMPLE**。下一句。")
    assert "AKIAIOSFODNN7EXAMPLE" not in "".join(phrases)


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
        MagicMock(
            provider="piper",
            piper_binary="piper",
            piper_model="voice.onnx",
            piper_model_config="",
            piper_length_scale=1.0,
        ),
    )
    return app, state


@pytest.mark.asyncio
async def test_request_identity_and_replay_keep_all_pcm(voice_app, monkeypatch):
    app, state = voice_app

    async def stream(*_args, **_kwargs):
        yield 0, 16000, b"\x01\x02"
        assert state.broadcast_ws.call_count == 1
        assert state.broadcast_ws.call_args.args[0] == "voice_chunk"
        yield 1, 16000, b"\x03\x04"

    monkeypatch.setattr(chat_voice, "streaming_piper_reply", stream)
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
    assert [call.args[0] for call in calls] == ["voice_chunk", "voice_chunk", "voice_complete"]
    assert all(call.args[1]["request_id"] == "request-1" for call in calls)
    replay = base64.b64decode(calls[-1].args[1]["audio"])
    with wave.open(io.BytesIO(replay)) as audio:
        assert audio.getframerate() == 16000
        assert audio.readframes(audio.getnframes()) == b"\x01\x02\x03\x04"


@pytest.mark.asyncio
async def test_cancel_closes_generator_and_frees_capacity(voice_app, monkeypatch):
    app, state = voice_app
    started = asyncio.Event()
    closed = asyncio.Event()

    async def stream(*_args, **_kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
            yield 0, 22050, b"\x00\x00"
        finally:
            closed.set()

    monkeypatch.setattr(chat_voice, "streaming_piper_reply", stream)
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
            await asyncio.wait_for(started.wait(), timeout=2)
            response = await client.post(
                "/api/voice/cancel", json={"slot": "s1", "request_id": "stop-me"}
            )
            assert response.status == 200
            await asyncio.wait_for(closed.wait(), timeout=2)
            assert not app[chat_voice._VOICE_REQUESTS].tasks
            state.broadcast_ws.assert_not_called()
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_before_synthesize_blocks_the_racing_request(voice_app, monkeypatch):
    app, state = voice_app
    synth = MagicMock()
    monkeypatch.setattr(chat_voice, "streaming_piper_reply", synth)
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
    monkeypatch.setattr(chat_voice, "streaming_piper_reply", synth)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/voice/synthesize", json=body)
        assert response.status == 400
        assert (await response.json())["code"] == (
            "body_not_object" if isinstance(body, list) else "voice_invalid_request"
        )
    synth.assert_not_called()
