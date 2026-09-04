"""Resident voices reuse native state without guessing when a request ends."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import piper_runtime as runtime
from kiro_crew import piper_worker as worker
from kiro_crew import voice_reply as voice


def frame(kind, identity="", pcm=b"", **meta):
    result = io.BytesIO()
    worker.write_frame(result, kind, identity, pcm, **meta)
    return result.getvalue()


def read_frames(data):
    source = io.BytesIO(data)
    frames = []
    while sizes := source.read(8):
        header_size, pcm_size = struct.unpack("!II", sizes)
        frames.append((json.loads(source.read(header_size)), source.read(pcm_size)))
    return frames


def test_frozen_executable_never_selected_as_python_worker(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    runtime.python_piper_available.cache_clear()
    try:
        assert runtime.python_piper_available() is False
    finally:
        runtime.python_piper_available.cache_clear()


class FakeWorker:
    def __init__(self, *, respond=True):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = MagicMock(drain=AsyncMock(), write=self.request)
        self.returncode = None
        self.requests = []
        self.respond = respond
        self.killed = self.reaped = False
        self.sent = asyncio.Event()
        self.stdout.feed_data(frame("ready", sample_rate=22050))

    def request(self, data):
        request = json.loads(data)
        self.requests.append(request)
        self.sent.set()
        if self.respond:
            self.answer(request["id"])

    def answer(self, identity):
        self.stdout.feed_data(frame("pcm", identity, b"\x01\x00" * 4410, sample_rate=22050))
        self.stdout.feed_data(frame("done", identity))

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def communicate(self):
        self.reaped = True
        return b"", b""


@pytest.fixture
def runtime_env(tmp_path, monkeypatch):
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"model")
    config = tmp_path / "voice.onnx.json"
    config.write_text(json.dumps({"audio": {"sample_rate": 22050}}), encoding="utf-8")
    cleanup = tmp_path / "sandbox-launcher"
    wrap = AsyncMock(side_effect=lambda argv, **kw: (argv, str(cleanup)))
    monkeypatch.setattr(runtime, "wrap_argv_async", wrap)
    monkeypatch.setattr(runtime, "cgroup_scope_argv", lambda argv: argv)
    return {"model": str(model), "config": str(config), "sample_rate": 22050, "length_scale": 1.0}


async def collect(engine, settings, identity="request"):
    stream = engine.stream(["你好。"], request_id=identity, **settings)
    async with contextlib.aclosing(stream):
        return [chunk async for chunk in stream]


@pytest.mark.asyncio
async def test_two_requests_share_loaded_worker_and_finish_without_eof(runtime_env, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-only-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-only-token")
    proc = FakeWorker()
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(runtime, "create_subprocess_limited", spawn)
    engine = runtime.PiperRuntime()
    try:
        first = await asyncio.wait_for(collect(engine, runtime_env, "first"), 2)
        second = await asyncio.wait_for(collect(engine, runtime_env, "second"), 2)
        assert first == second and first[0][:2] == (0, 22050)
        assert [r["id"] for r in proc.requests] == ["first", "second"]
        assert spawn.call_count == 1
        child_env = spawn.call_args.kwargs["env"]
        assert "AWS_SECRET_ACCESS_KEY" not in child_env
        assert "SLACK_BOT_TOKEN" not in child_env
        assert child_env["PYTHONIOENCODING"] == "utf-8"
        assert child_env["PYTHONUNBUFFERED"] == "1"
        assert not proc.stdout.at_eof() and not proc.killed
        assert runtime.wrap_argv_async.call_args.kwargs["mode"] == "standard"
        assert "first_party_fixed_argv" not in runtime.wrap_argv_async.call_args.kwargs
    finally:
        await asyncio.wait_for(engine.close(), 2)
    assert proc.killed and proc.reaped


@pytest.mark.asyncio
async def test_changed_model_retires_previous_instance(runtime_env, monkeypatch):
    first, second = FakeWorker(), FakeWorker()
    spawn = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(runtime, "create_subprocess_limited", spawn)
    engine = runtime.PiperRuntime()
    try:
        await collect(engine, runtime_env)
        with open(runtime_env["model"], "ab") as target:
            target.write(b"changed")
        await collect(engine, runtime_env)
        assert first.killed and first.reaped
        assert spawn.call_count == 2 and not second.killed
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_kill_active_request(runtime_env, monkeypatch):
    proc = FakeWorker(respond=False)
    monkeypatch.setattr(runtime, "create_subprocess_limited", AsyncMock(return_value=proc))
    engine = runtime.PiperRuntime()
    active = asyncio.create_task(collect(engine, runtime_env, "active"))
    try:
        await asyncio.wait_for(proc.sent.wait(), 2)
        waiting = asyncio.create_task(collect(engine, runtime_env, "waiting"))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not proc.killed and [r["id"] for r in proc.requests] == ["active"]
        proc.answer("active")
        assert await asyncio.wait_for(active, 2)
    finally:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)
        await engine.close()


@pytest.mark.asyncio
async def test_cancel_active_request_reaps_and_next_request_reloads(runtime_env, monkeypatch):
    first, second = FakeWorker(respond=False), FakeWorker()
    monkeypatch.setattr(
        runtime, "create_subprocess_limited", AsyncMock(side_effect=[first, second])
    )
    engine = runtime.PiperRuntime()
    active = asyncio.create_task(collect(engine, runtime_env))
    try:
        await asyncio.wait_for(first.sent.wait(), 2)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(active, 2)
        assert first.killed and first.reaped
        assert await collect(engine, runtime_env)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_early_generator_close_retires_worker(runtime_env, monkeypatch):
    proc = FakeWorker()
    monkeypatch.setattr(runtime, "create_subprocess_limited", AsyncMock(return_value=proc))
    engine = runtime.PiperRuntime()
    stream = engine.stream(["hello"], **runtime_env)
    async with contextlib.aclosing(stream):
        assert await asyncio.wait_for(anext(stream), 2)
    assert proc.killed and proc.reaped
    await engine.close()


@pytest.mark.asyncio
async def test_idle_and_shutdown_reap_workers(runtime_env, monkeypatch):
    first, second = FakeWorker(), FakeWorker(respond=False)
    monkeypatch.setattr(
        runtime, "create_subprocess_limited", AsyncMock(side_effect=[first, second])
    )
    engine = runtime.PiperRuntime(idle_seconds=0.01)
    await collect(engine, runtime_env)
    await asyncio.wait_for(asyncio.shield(engine._idle), 2)
    assert first.killed and first.reaped
    active = asyncio.create_task(collect(engine, runtime_env))
    await asyncio.wait_for(second.sent.wait(), 2)
    await asyncio.wait_for(engine.close(), 2)
    assert active.cancelled() and second.killed and second.reaped


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["identity", "oversize", "partial", "format", "empty", "error"])
async def test_invalid_protocol_is_bounded_and_reaped(runtime_env, monkeypatch, bad):
    proc = FakeWorker(respond=False)

    def answer(data):
        identity = json.loads(data)["id"]
        payloads = {
            "identity": frame("pcm", "obsolete", b"\0\0", sample_rate=22050),
            "oversize": struct.pack("!II", worker.MAX_HEADER_BYTES + 1, 0),
            "partial": b"\0",
            "format": frame("pcm", identity, b"\0", sample_rate=22050),
            "empty": frame("done", identity),
            "error": frame("error", identity, code="voice_synthesis_failed"),
        }
        proc.stdout.feed_data(payloads[bad])
        if bad == "partial":
            proc.stdout.feed_eof()

    proc.stdin.write = answer
    monkeypatch.setattr(runtime, "create_subprocess_limited", AsyncMock(return_value=proc))
    engine = runtime.PiperRuntime()
    try:
        with pytest.raises(voice.VoiceSynthesisError):
            await asyncio.wait_for(collect(engine, runtime_env), 2)
        assert proc.killed and proc.reaped
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_spawn", "after_request"])
async def test_timeout_reaps(runtime_env, monkeypatch, phase):
    proc = FakeWorker(respond=False)
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(runtime, "create_subprocess_limited", spawn)
    # A 20 ms wall deadline could expire during off-loop file stat under load,
    # before any worker existed. Trigger the real timeout at a known lifecycle
    # boundary so this tests ownership/cleanup rather than scheduler speed.
    deadline = asyncio.timeout(None)
    monkeypatch.setattr(runtime.asyncio, "timeout", lambda _seconds: deadline)
    reached = proc.sent
    if phase == "before_spawn":
        reached = asyncio.Event()

        async def preparing(*args, **kwargs):
            reached.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(runtime, "wrap_argv_async", preparing)
    engine = runtime.PiperRuntime()
    task = asyncio.create_task(collect(engine, runtime_env))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        deadline.reschedule(asyncio.get_running_loop().time())
        with pytest.raises(voice.VoiceSynthesisError, match="timed out") as error:
            await asyncio.wait_for(task, 2)
        assert error.value.code == "voice_timeout"
        if phase == "after_request":
            spawn.assert_awaited_once()
            assert proc.killed and proc.reaped
        else:
            spawn.assert_not_awaited()
            assert not proc.killed and not proc.reaped
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await engine.close()


@pytest.mark.asyncio
async def test_sandbox_refusal_never_spawns(runtime_env, monkeypatch):
    monkeypatch.setattr(
        runtime,
        "wrap_argv_async",
        AsyncMock(side_effect=runtime.SandboxUnavailableError("no backend", "no_backend", "test")),
    )
    spawn = AsyncMock()
    monkeypatch.setattr(runtime, "create_subprocess_limited", spawn)
    engine = runtime.PiperRuntime()
    try:
        with pytest.raises(voice.VoiceSynthesisError) as error:
            await collect(engine, runtime_env)
        assert error.value.code == "voice_sandbox_unavailable"
        spawn.assert_not_called()
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_python_api_path_does_not_require_cli(runtime_env, monkeypatch):
    monkeypatch.setattr(runtime, "python_piper_available", lambda: True)
    monkeypatch.setattr(voice, "_resolve_piper_binary", lambda _binary: None)
    proc = FakeWorker()
    monkeypatch.setattr(runtime, "create_subprocess_limited", AsyncMock(return_value=proc))
    engine = runtime.PiperRuntime()
    stream = voice.streaming_piper_reply(
        "第一句。第二句", piper_model=runtime_env["model"], runtime=engine, request_id="api"
    )
    try:
        async with contextlib.aclosing(stream):
            assert [part async for part in stream]
        assert proc.requests[0]["phrases"] == ["第一句。", "第二句"]
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_binary,has_api", [("custom-piper", True), ("", False)])
async def test_explicit_binary_or_missing_api_preserves_cli(
    runtime_env, monkeypatch, explicit_binary, has_api
):
    monkeypatch.setattr(runtime, "python_piper_available", lambda: has_api)
    monkeypatch.setattr(voice, "_resolve_piper_binary", lambda _binary: None)
    engine = MagicMock()
    stream = voice.streaming_piper_reply(
        "hello", piper_model=runtime_env["model"], piper_binary=explicit_binary, runtime=engine
    )
    with pytest.raises(voice.VoiceSynthesisError, match="unavailable"):
        await anext(stream)
    engine.stream.assert_not_called()


def test_worker_frames_two_requests_with_same_voice_and_unicode_id():
    spoken = []
    pcm = b"\x01\x00" * 5000

    def synthesize(text, config):
        spoken.append((text, config.length_scale))
        yield SimpleNamespace(
            sample_rate=22050, sample_width=2, sample_channels=1, audio_int16_bytes=pcm
        )

    fake_voice = SimpleNamespace(config=SimpleNamespace(sample_rate=22050), synthesize=synthesize)
    ids = ["first", "音" * 128]
    requests = b"".join(
        (json.dumps({"id": id_, "phrases": ["hello"], "length_scale": 1.25}) + "\n").encode()
        for id_ in ids
    )
    output = io.BytesIO()
    assert worker.serve(fake_voice, SimpleNamespace, io.BytesIO(requests), output) == 0
    frames = read_frames(output.getvalue())
    assert frames[0][0]["type"] == "ready"
    assert [meta["id"] for meta, _ in frames if meta["type"] == "done"] == ids
    for identity in ids:
        assert b"".join(data for meta, data in frames if meta.get("id") == identity) == pcm
    assert spoken == [("hello", 1.25), ("hello", 1.25)]


@pytest.mark.parametrize(
    "fault",
    ["json", "unterminated", "oversize", "identity", "scale", "text_limit", "phrases"],
)
def test_worker_retires_on_bad_request_without_processing_following_text(fault):
    request = {"id": "bad", "phrases": ["private requested text"], "length_scale": 1.0}
    if fault == "identity":
        request["id"] = 3
    elif fault == "scale":
        request["length_scale"] = float("nan")
    elif fault == "text_limit":
        request["phrases"] = ["a" * 240] * 84
    elif fault == "phrases":
        request["phrases"] = "private requested text"
    line = (json.dumps(request) + "\n").encode()
    if fault == "json":
        line = b"invalid private requested text\n"
    elif fault == "unterminated":
        line = line.rstrip(b"\n")
    elif fault == "oversize":
        line = b"a" * (worker.MAX_REQUEST_BYTES + 1) + b"\n"
    # A bad request is terminal: the next valid request must not be synthesized.
    following = b'{"id":"next","phrases":["next text"],"length_scale":1}\n'
    source = io.BytesIO(line if fault == "unterminated" else line + following)
    synthesize = MagicMock()
    fake_voice = SimpleNamespace(config=SimpleNamespace(sample_rate=22050), synthesize=synthesize)
    output = io.BytesIO()
    assert worker.serve(fake_voice, SimpleNamespace, source, output) == 1
    frames = read_frames(output.getvalue())
    assert [meta["type"] for meta, _ in frames] == ["ready", "error"]
    assert frames[-1][0]["code"] == "voice_synthesis_failed"
    assert b"private requested text" not in output.getvalue()
    synthesize.assert_not_called()


@pytest.mark.parametrize("fault", ["format", "odd", "budget", "empty", "exception"])
def test_worker_provider_fault_is_terminal_and_never_reports_done(monkeypatch, fault):
    pcm = b"\x01\x00" * 3
    if fault == "odd":
        pcm += b"\x01"
    elif fault == "budget":
        monkeypatch.setattr(worker, "MAX_AUDIO_BYTES", len(pcm) - 1)
    elif fault == "empty":
        pcm = b""

    def synthesize(_text, _config):
        if fault == "exception":
            raise RuntimeError("private native diagnostic")
        yield SimpleNamespace(
            sample_rate=22050,
            sample_width=2,
            sample_channels=2 if fault == "format" else 1,
            audio_int16_bytes=pcm,
        )

    fake_voice = SimpleNamespace(config=SimpleNamespace(sample_rate=22050), synthesize=synthesize)
    source = io.BytesIO(b'{"id":"bad","phrases":["text"],"length_scale":1}\n')
    output = io.BytesIO()
    assert worker.serve(fake_voice, SimpleNamespace, source, output) == 1
    frames = read_frames(output.getvalue())
    assert [meta["type"] for meta, _ in frames] == ["ready", "error"]
    assert frames[-1][0]["id"] == "bad"
    assert b"private native diagnostic" not in output.getvalue()


@pytest.mark.parametrize("load_fails", [False, True])
def test_worker_entrypoint_loads_once_and_reports_model_failure(monkeypatch, load_fails):
    chunk = SimpleNamespace(
        sample_rate=22050, sample_width=2, sample_channels=1, audio_int16_bytes=b"\x01\x00"
    )
    fake_voice = SimpleNamespace(
        config=SimpleNamespace(sample_rate=22050), synthesize=lambda *_: iter([chunk])
    )
    loader = MagicMock(
        return_value=fake_voice,
        side_effect=ValueError("private model load diagnostic") if load_fails else None,
    )
    fake_piper = SimpleNamespace(
        PiperVoice=SimpleNamespace(load=loader), SynthesisConfig=SimpleNamespace
    )
    source = io.BytesIO(b'{"id":"one","phrases":["text"],"length_scale":1}\n')
    output = io.BytesIO()
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "piper", fake_piper)
        patch.setattr(sys, "argv", ["piper_worker", "--model", "voice", "--config", "config"])
        patch.setattr(sys, "stdin", SimpleNamespace(buffer=source))
        patch.setattr(sys, "stdout", SimpleNamespace(buffer=output))
        result = worker.main()
    loader.assert_called_once_with("voice", config_path="config")
    frames = read_frames(output.getvalue())
    assert result == (1 if load_fails else 0)
    assert [meta["type"] for meta, _ in frames] == (
        ["error"] if load_fails else ["ready", "pcm", "done"]
    )
    assert b"private model load diagnostic" not in output.getvalue()


@pytest.mark.asyncio
async def test_caller_cancelled_between_yields_closes_nested_runtime(runtime_env, monkeypatch):
    proc = FakeWorker()
    monkeypatch.setattr(runtime, "create_subprocess_limited", AsyncMock(return_value=proc))
    monkeypatch.setattr(runtime, "python_piper_available", lambda: True)
    engine = runtime.PiperRuntime()
    received = asyncio.Event()

    async def consume():
        stream = voice.streaming_piper_reply(
            "hello", piper_model=runtime_env["model"], runtime=engine
        )
        async with contextlib.aclosing(stream):
            async for _ in stream:
                received.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(received.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert proc.killed and proc.reaped
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_real_worker_protocol_reuses_process_and_reaps_on_close(runtime_env, monkeypatch):
    # Run the real framing loop through OS pipes, with fixed fake synthesis so
    # this regression needs neither an installed Piper package nor a model.
    code = """
import sys
from types import SimpleNamespace
from kiro_crew.piper_worker import serve
def synthesize(text, config):
    yield SimpleNamespace(sample_rate=22050, sample_width=2,
        sample_channels=1, audio_int16_bytes=b'\\x01\\x00' * 5000)
voice = SimpleNamespace(config=SimpleNamespace(sample_rate=22050), synthesize=synthesize)
raise SystemExit(serve(voice, SimpleNamespace, sys.stdin.buffer, sys.stdout.buffer))
"""
    processes = []

    async def spawn(*argv, **kwargs):
        proc = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c", code, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(runtime, "create_subprocess_limited", spawn)
    engine = runtime.PiperRuntime()
    try:
        for identity in ("one", "two"):
            chunks = await asyncio.wait_for(collect(engine, runtime_env, identity), 5)
            assert b"".join(part[2] for part in chunks) == b"\x01\x00" * 5000
        assert len(processes) == 1 and processes[0].returncode is None
    finally:
        await asyncio.wait_for(engine.close(), 5)
    assert processes[0].returncode is not None


@pytest.mark.asyncio
async def test_repeated_cancel_during_reap_keeps_cleanup_owned(runtime_env, monkeypatch):
    proc = FakeWorker(respond=False)
    cleanup = Path(runtime_env["model"]).parent / "sandbox-launcher"
    cleanup.write_text("profile", encoding="utf-8")
    reaping = asyncio.Event()
    release_reap = asyncio.Event()

    async def communicate():
        reaping.set()
        await release_reap.wait()
        proc.reaped = True
        return b"", b""

    proc.communicate = communicate
    monkeypatch.setattr(runtime, "create_subprocess_limited", AsyncMock(return_value=proc))
    engine = runtime.PiperRuntime()
    task = asyncio.create_task(collect(engine, runtime_env))
    try:
        await asyncio.wait_for(proc.sent.wait(), 2)
        task.cancel()
        await asyncio.wait_for(reaping.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert engine._reaping is not None and not engine._reaping.done()
        release_reap.set()
        await asyncio.wait_for(engine.close(), 2)
        assert proc.killed and proc.reaped and not cleanup.exists()
    finally:
        release_reap.set()
        await engine.close()
