"""Codex voice contract tests: no Codex installation, account, microphone or network."""

from __future__ import annotations

import asyncio
import importlib.abc
import importlib.machinery
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import transcribe
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import client_spawn_env
from kiro_crew.config.sections_stt import SttConfig, _validated_stt_provider
from kiro_crew.stt import codex, codex_media, codex_rpc


def _cli(argv):
    """A ``codex_cli`` stand-in: *argv* found, or no adapter installed."""
    reason = "" if argv else client_spawn_env.CODEX_CLI_ADAPTER_MISSING
    return lambda: client_spawn_env.CodexCliResolution(argv, "/searched", reason)


def _fake_sandbox(monkeypatch, launch, directory=None, configured="auto", masked=True):
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
            credential_mask_applies=lambda mode: masked,
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
        await rpc.start(["/installed/codex"])
    assert wrap.await_args.kwargs["mode"] == expected
    assert "require_confinement" not in wrap.await_args.kwargs
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
        (["/installed/codex"], None, codex.CODE_EXTRA),
        (["/installed/codex"], object(), ""),
    ],
)
def test_local_prerequisites_never_contact_codex(monkeypatch, binary, extra, code):
    monkeypatch.setattr(codex, "codex_cli", _cli(binary))
    monkeypatch.setattr(codex.importlib.util, "find_spec", lambda name: extra)
    monkeypatch.setattr(codex.sandbox, "credential_mask_applies", lambda mode: True)
    verdict = codex.availability()
    assert verdict.code == code
    assert verdict.ok is (code == "")


def _refuses(mode):
    raise OSError("probe failed")


@pytest.mark.parametrize(
    "probe,code",
    [
        (lambda mode: False, codex.CODE_SANDBOX),
        (_refuses, codex.CODE_SANDBOX),
        (lambda mode: True, ""),
    ],
)
def test_availability_reports_a_host_the_sandbox_would_refuse(monkeypatch, probe, code):
    seen = []
    monkeypatch.setattr(codex, "codex_cli", _cli(["/installed/codex"]))
    monkeypatch.setattr(codex.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(codex.codex_rpc, "sandbox_mode", lambda: "strict")

    def check(mode):
        seen.append(mode)
        return probe(mode)

    monkeypatch.setattr(codex.sandbox, "credential_mask_applies", check)
    verdict = codex.availability()
    assert (verdict.ok, verdict.code) == (code == "", code)
    assert seen == ["strict"]


@pytest.mark.parametrize(
    "binary,extra",
    [
        (lambda: None, None),
        (lambda: ["/installed/codex"], None),
        (lambda: None, object()),
    ],
    ids=["no-binary-no-extra", "no-extra", "no-binary"],
)
def test_sandbox_refusal_is_reported_before_any_install_step(monkeypatch, binary, extra):
    # On a host with no confining backend no install step can make Codex run, so
    # the refusal must win over the binary and extra checks.
    from kiro_crew import sandbox

    monkeypatch.setattr(codex, "codex_cli", _cli(binary()))
    monkeypatch.setattr(codex.importlib.util, "find_spec", lambda name: extra)
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: False)
    verdict = codex.availability()
    assert (verdict.ok, verdict.code) == (False, codex.CODE_SANDBOX)


def test_extra_missing_message_names_no_install_command(monkeypatch):
    # The prerequisites panel owns the command, and suppresses it where pip cannot
    # target this interpreter; the message must not repeat it in any locale.
    from kiro_crew import sandbox

    locales = Path(__file__).resolve().parents[1] / "website" / "src" / "i18n" / "locales"
    files = sorted(p for p in locales.glob("*.json") if p.name != "en.json")
    assert len(files) == 13
    for path in files:
        text = json.loads(path.read_text(encoding="utf-8"))["pages"]["settings"]["sttSettings"][
            "unavailable_codex_extra"
        ]
        assert "pip install" not in text, path.name
        assert "[voice-codex]" not in text and "[ṽøìçè-çøðèẋ]" not in text, path.name
    monkeypatch.setattr(codex, "codex_cli", _cli(["/installed/codex"]))
    monkeypatch.setattr(codex.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True)
    verdict = codex.availability()
    assert verdict.code == codex.CODE_EXTRA
    assert "pip" not in verdict.detail


@pytest.mark.parametrize(
    "backend,floor,configured,inside,refused",
    [
        ("namespace", None, "standard", False, False),
        ("namespace", None, "off", False, False),
        ("none", None, "standard", False, True),
        ("none", "strict", "standard", False, True),
        # Already inside a Crew sandbox: the nested passthrough would drop the mask.
        ("namespace", None, "standard", True, True),
    ],
)
def test_settings_refuses_exactly_where_the_provider_mask_would_not_apply(
    monkeypatch,
    backend,
    floor,
    configured,
    inside,
    refused,
):
    # The provider's own predicate decides, read at the tier the child runs at.
    # Patched through the facade: these names have several holders, and only the
    # facade's setattr reaches all of them (test_facade_mirror_guards).
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: floor)
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: inside)
    monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: None)
    monkeypatch.setattr(sandbox, "detect_backend", lambda config_mode="auto": backend)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: configured)
    assert codex.sandbox_refuses() is refused
    assert refused is not sandbox.credential_mask_applies(codex_rpc.sandbox_mode())


@pytest.mark.asyncio
async def test_start_refuses_before_spawning_where_the_mask_would_not_apply(monkeypatch):
    launch = AsyncMock()
    wrap, _ = _fake_sandbox(monkeypatch, launch, masked=False)
    monkeypatch.setattr(codex_rpc.platform_compat, "kill_and_reap", AsyncMock())
    rpc = codex_rpc.CodexRpc()
    rpc.call = AsyncMock()
    with pytest.raises(codex_rpc.CodexError) as error:
        await rpc.start(["/installed/codex"])
    assert error.value.code == codex_rpc.CODE_SANDBOX
    wrap.assert_not_awaited()
    launch.assert_not_awaited()
    rpc.call.assert_not_awaited()
    assert rpc.directory is None


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


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _adapter(monkeypatch, argv):
    monkeypatch.delenv("CODEX_PATH", raising=False)
    monkeypatch.setattr(acp_client, "_resolve_codex_acp_bin", lambda: (argv, "/searched"))


def test_codex_path_override_wins_over_the_adapter(monkeypatch, tmp_path):
    binary = _touch(tmp_path / "custom" / ("codex.exe" if os.name == "nt" else "codex"))
    binary.chmod(0o755)
    monkeypatch.setenv("CODEX_PATH", str(binary))

    def adapter():
        raise AssertionError("CODEX_PATH must win without resolving the adapter")

    monkeypatch.setattr(acp_client, "_resolve_codex_acp_bin", adapter)
    assert codex.codex_argv() == [str(binary)]


def _no_adapter(monkeypatch):
    def adapter():
        raise AssertionError("a set CODEX_PATH must not fall through to the adapter")

    monkeypatch.setattr(acp_client, "_resolve_codex_acp_bin", adapter)
    monkeypatch.setattr(codex.sandbox, "credential_mask_applies", lambda mode: True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute bit")
def test_relative_codex_path_resolves_against_the_gateway_cwd(monkeypatch, tmp_path):
    # The dictation child is spawned from an empty temp dir, so a relative path
    # would stop resolving there; the argv must already be absolute.
    _touch(tmp_path / "bin" / "codex").chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_PATH", os.path.join("bin", "codex"))
    _no_adapter(monkeypatch)
    assert codex.codex_argv() == [str(tmp_path / "bin" / "codex")]


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute bit")
def test_non_executable_codex_path_is_refused_with_the_reason(monkeypatch, tmp_path):
    binary = _touch(tmp_path / "codex")
    binary.chmod(0o644)
    monkeypatch.setenv("CODEX_PATH", str(binary))
    _no_adapter(monkeypatch)
    assert codex.codex_argv() is None
    verdict = codex.availability()
    assert verdict.code == codex.CODE_MISSING
    assert f"CODEX_PATH={binary} is not executable." in verdict.detail


def test_missing_codex_path_file_is_refused_with_the_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_PATH", str(tmp_path / "absent"))
    _no_adapter(monkeypatch)
    verdict = codex.availability()
    assert verdict.code == codex.CODE_MISSING
    assert "is not a file." in verdict.detail


@pytest.mark.parametrize("suffix", [".mjs", ".cjs", ".js"])
def test_script_codex_path_is_wrapped_with_node(monkeypatch, tmp_path, suffix):
    script = _touch(tmp_path / f"codex{suffix}")
    monkeypatch.setenv("CODEX_PATH", str(script))
    _no_adapter(monkeypatch)
    monkeypatch.setattr(
        client_spawn_env.launch_mod, "_resolve_node_for_script", lambda path: "/fixture/node"
    )
    assert codex.codex_argv() == ["/fixture/node", os.path.realpath(script)]


def test_adapter_dependency_resolves_to_node_and_codex_script(monkeypatch, tmp_path):
    package = tmp_path / "node_modules" / "@agentclientprotocol" / "codex-acp"
    entry = _touch(package / "dist" / "index.js")
    script = _touch(package / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")
    _adapter(monkeypatch, ["/fixture/node", str(entry)])
    assert codex.codex_argv() == ["/fixture/node", os.path.realpath(script)]


def test_npm_bin_link_adapter_uses_the_ladder_node(monkeypatch, tmp_path):
    entry = _touch(tmp_path / "lib" / "node_modules" / "codex-acp" / "dist" / "index.js")
    script = _touch(tmp_path / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")
    _adapter(monkeypatch, [str(entry)])
    monkeypatch.setattr(client_spawn_env.launch_mod, "_resolve_node_for_script", lambda path: None)
    monkeypatch.setattr(client_spawn_env.shutil, "which", lambda name, path=None: "/fixture/node")
    assert codex.codex_argv() == ["/fixture/node", os.path.realpath(script)]


def test_nothing_installed_reports_the_adapter_install(monkeypatch):
    _adapter(monkeypatch, None)
    monkeypatch.setattr(codex.sandbox, "credential_mask_applies", lambda mode: True)
    assert codex.codex_argv() is None
    verdict = codex.availability()
    assert verdict.code == codex.CODE_MISSING
    assert "@agentclientprotocol/codex-acp" in verdict.detail
    assert "CODEX_PATH" in verdict.detail


@pytest.mark.parametrize("shape", ["shim", "node-entry-without-codex"])
def test_adapter_without_a_locatable_codex_is_not_reported_missing(monkeypatch, tmp_path, shape):
    # A Volta or asdf launcher shim is a native file whose realpath has no
    # node_modules tree; the adapter is installed, so "install the adapter" is wrong.
    if shape == "shim":
        argv = [str(_touch(tmp_path / "volta" / "bin" / "codex-acp"))]
    else:
        argv = ["/fixture/node", str(_touch(tmp_path / "node_modules" / "codex-acp" / "index.js"))]
    _adapter(monkeypatch, argv)
    monkeypatch.setattr(codex.sandbox, "credential_mask_applies", lambda mode: True)
    assert codex.codex_argv() is None
    verdict = codex.availability()
    assert verdict.code == codex.CODE_CLI_UNRESOLVED == "stt_codex_cli_unresolved"
    assert "CODEX_PATH" in verdict.detail
    assert "@agentclientprotocol/codex-acp" not in verdict.detail


def test_oversized_transcript_item_id_is_rejected():
    session = codex.StreamingSession()
    long_id = "x" * (codex._MAX_ITEM_ID_CHARS + 1)
    with pytest.raises(ValueError):
        session._data_event({"type": "input_transcript.added", "item": {"id": long_id, "text": ""}})
    assert session.seen == set()


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
    monkeypatch.setattr(codex, "codex_argv", lambda: ["/installed/codex"])
    monkeypatch.setattr(codex_media, "load_media_stack", lambda: None)
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
async def test_start_imports_media_stack_off_loop_and_builds_peer_on_loop(monkeypatch, wire):
    loop_thread = threading.get_ident()
    imported = {}

    class FakeMediaModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        """Serve empty av and aiortc modules and record the importing thread."""

        def find_spec(self, fullname, path, target=None):
            if fullname in {"av", "aiortc"}:
                return importlib.machinery.ModuleSpec(fullname, self)
            return None

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            imported[module.__name__] = threading.get_ident()

    for name in ("av", "aiortc"):
        # setitem first records the original entry, or its absence, so teardown
        # also removes the fake module when the real one was never imported.
        monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [FakeMediaModules(), *sys.meta_path])

    peer = FakePeer()
    track = SimpleNamespace(stop=lambda: None)
    built = {}

    def create_peer():
        built["thread"] = threading.get_ident()
        built["imported"] = set(imported)
        return peer, track

    monkeypatch.setattr(codex, "availability", lambda: codex.Availability(True))
    monkeypatch.setattr(codex, "codex_argv", lambda: ["/installed/codex"])
    monkeypatch.setattr(codex_media, "create_peer", create_peer)
    monkeypatch.setattr(codex_media, "remote_answer", lambda value: value)
    session = codex.StreamingSession()
    session.rpc.start = AsyncMock()
    session.rpc.close = AsyncMock()
    session.rpc.directory = SimpleNamespace(name="/voice-fixture-directory")

    async def call(method, params):
        if method == "thread/start":
            return wire["thread"]
        if method == "thread/realtime/start":
            session.rpc.events.put_nowait(wire["sdp"])
        return {}

    session.rpc.call = call
    try:
        assert await session.start() is None
    finally:
        await session.close()
    assert set(imported) == {"av", "aiortc"}
    assert all(thread != loop_thread for thread in imported.values())
    assert built == {"thread": loop_thread, "imported": {"av", "aiortc"}}


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
    monkeypatch.setattr(codex, "codex_argv", lambda: ["/installed/codex"])
    monkeypatch.setattr(codex_media, "load_media_stack", lambda: None)
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
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True)
    monkeypatch.setattr(codex_rpc.platform_compat, "kill_and_reap", AsyncMock())
    monkeypatch.setattr(codex, "availability", lambda: codex.Availability(True))
    monkeypatch.setattr(codex, "codex_argv", lambda: ["/installed/codex"])
    peer = FakePeer()
    track = SimpleNamespace(stop=lambda: None)
    monkeypatch.setattr(codex_media, "load_media_stack", lambda: None)
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
    await session._rpc_events()
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
    await session._rpc_events()
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
    codex_cli = ["/fixture/node", "/fixture/node_modules/@openai/codex/bin/codex.js"]
    rpc = codex_rpc.CodexRpc()
    try:
        if account_type == "chatgpt":
            await rpc.start(codex_cli)
        else:
            with pytest.raises(codex_rpc.CodexError) as error:
                await rpc.start(codex_cli)
            assert error.value.code == codex_rpc.CODE_LOGIN
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX
        from kiro_crew.agent_sdk.tool_gate import adapter_hidden_credential_dirs

        wrapped = wrap.await_args.args[0]
        assert wrapped == [
            *codex_cli,
            "app-server",
            "--listen",
            "stdio://",
            "--disable",
            "shell_tool",
            "-c",
            "mcp_servers={}",
        ]
        assert wrap.await_args.kwargs["mode"] == "standard"
        assert wrap.await_args.kwargs["strip_python_env"] is True
        # The real provider mask, not a stand-in: dropping it must fail this test.
        mask = tuple(adapter_hidden_credential_dirs(ACP_BACKEND_CODEX))
        assert mask
        assert wrap.await_args.kwargs["extra_hidden_dirs"] == mask
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
            credential_mask_applies=lambda mode: True,
        ),
    )
    rpc = codex_rpc.CodexRpc()
    with pytest.raises(codex_rpc.CodexError) as error:
        await rpc.start(["/installed/codex"])
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


@pytest.mark.parametrize(
    ("agent", "version"),
    [
        ("kiro_crew_dictation/0.163.0 (fixture)", (0, 163, 0)),
        ("kiro_crew_dictation/1.2.3", (1, 2, 3)),
        ("kiro_crew_dictation/0.163.0\t(tab)", (0, 163, 0)),
        ("kiro_crew_dictation/0.163.0-alpha", None),
        ("kiro_crew_dictation/0.163.0.1", None),
        ("kiro_crew_dictation/0.163", None),
        ("kiro_crew_dictation/0..0", None),
        ("kiro_crew_dictation/0.１.0", None),
        ("codex_cli_rs/0.163.0", None),
        ("other kiro_crew_dictation/0.163.0", None),
        ("unknown", None),
        ("", None),
        (None, None),
    ],
)
def test_codex_version_reads_only_an_exact_three_part_token(agent, version):
    assert codex_rpc.codex_version(agent) == version


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent",
    [
        "kiro_crew_dictation/0.162.0 (fixture)",
        "unknown",
        "kiro_crew_dictation/0.163.0-alpha",
        "kiro_crew_dictation/0.163.0.1",
        "codex_cli_rs/0.163.0",
    ],
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
        # The adapter's bundled Codex runs as node + script; the gate reads the
        # initialize reply, so that spawn shape cannot skip it.
        await rpc.start(["/fixture/node", "/fixture/@openai/codex/bin/codex.js"])
    assert error.value.code == codex_rpc.CODE_UNAVAILABLE
    assert rpc.call.await_count == 1
    assert rpc.process is None
