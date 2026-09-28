"""``kirocrew acp`` entrypoint: registration, jail scope, stdout discipline."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import cli_acp
from kiro_crew.cli import _JAILED_COMMANDS, main
from kiro_crew.cli_acp import _configure_logging


class TestSubcommandRegistration:
    def test_acp_is_registered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["kirocrew", "acp", "--help"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0


class TestJailScope:
    def test_acp_is_jailed(self) -> None:
        """``acp`` drives kiro-cli locally, so it must be jailed like chat/tui/run.

        ``gateway`` is excluded from the jail only because its dashboard binds
        inside the jail's private netns and would be unreachable; a stdio server
        has no inbound port, so that exemption does not apply here. Dropping
        ``acp`` from this set would run an agent with full tool access outside the
        isolation boundary.
        """
        assert "acp" in _JAILED_COMMANDS


class TestLoggingDiscipline:
    def test_logging_goes_to_stderr_not_stdout(self) -> None:
        """stdout carries JSON-RPC frames; a log line there corrupts the stream."""
        root = logging.getLogger()
        saved = list(root.handlers)
        saved_level = root.level
        try:
            _configure_logging(verbose=False)
            streams = [
                getattr(h, "stream", None)
                for h in logging.getLogger().handlers
                if isinstance(h, logging.StreamHandler)
            ]
            assert streams, "expected a StreamHandler"
            assert sys.stdout not in streams
            assert sys.stderr in streams
        finally:
            root.handlers = saved
            root.setLevel(saved_level)

    def test_verbose_sets_debug_level(self) -> None:
        root = logging.getLogger()
        saved = list(root.handlers)
        saved_level = root.level
        try:
            _configure_logging(verbose=True)
            assert logging.getLogger().level == logging.DEBUG
        finally:
            root.handlers = saved
            root.setLevel(saved_level)


class TestStdioStreams:
    """`_stdio_streams` wires real OS pipes, so exercise it on real OS pipes."""

    @pytest.mark.asyncio
    async def test_reads_from_stdin_pipe(self) -> None:
        r_fd, w_fd = os.pipe()
        with os.fdopen(r_fd, "rb", buffering=0) as rf, open(os.devnull, "wb") as sink:
            reader, _writer = await cli_acp._stdio_streams(stdin=rf, stdout=sink)
            os.write(w_fd, b'{"jsonrpc":"2.0","method":"ping","params":{}}\n')
            line = await asyncio.wait_for(reader.readline(), timeout=3)
            assert json.loads(line)["method"] == "ping"
            os.close(w_fd)

    @pytest.mark.asyncio
    async def test_writes_to_stdout_pipe(self) -> None:
        r_fd, w_fd = os.pipe()
        in_r, in_w = os.pipe()
        try:
            os.close(in_w)
            in_w = -1
            with (
                os.fdopen(in_r, "rb", buffering=0) as inf,
                os.fdopen(w_fd, "wb", buffering=0) as outf,
            ):
                _reader, writer = await cli_acp._stdio_streams(stdin=inf, stdout=outf)
                writer.write(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
                await writer.drain()
                echoed = await asyncio.wait_for(asyncio.to_thread(os.read, r_fd, 4096), timeout=3)
            assert json.loads(echoed)["id"] == 1
        finally:
            for fd in (in_w, r_fd):
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)

    @pytest.mark.asyncio
    async def test_windows_threaded_adapter_round_trip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli_acp.platform_compat, "IS_WINDOWS", True)
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        try:
            with (
                os.fdopen(in_r, "rb", buffering=0) as source,
                os.fdopen(out_w, "wb", buffering=0) as target,
            ):
                reader, writer = await cli_acp._stdio_streams(stdin=source, stdout=target)
                os.write(in_w, b'{"jsonrpc":"2.0","method":"ping"}\n')
                assert (
                    json.loads(await asyncio.wait_for(reader.readline(), timeout=3))["method"]
                    == "ping"
                )
                writer.write(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
                await writer.drain()
                echoed = await asyncio.wait_for(asyncio.to_thread(os.read, out_r, 4096), timeout=3)
                assert json.loads(echoed)["id"] == 1
                os.close(in_w)
                in_w = -1
                assert await asyncio.wait_for(reader.read(), timeout=3) == b""
        finally:
            for fd in (in_w, out_r):
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)

    @pytest.mark.asyncio
    async def test_threaded_writer_empty_drain_does_not_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = MagicMock()
        stream.fileno.return_value = 123
        writer = cli_acp._ThreadedPipeWriter(stream)
        write_all = MagicMock()
        monkeypatch.setattr(writer, "_write_all", write_all)

        await writer.drain()

        write_all.assert_not_called()

    def test_threaded_writer_retries_partial_descriptor_writes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = MagicMock()
        stream.fileno.return_value = 123
        writer = cli_acp._ThreadedPipeWriter(stream)
        os_write = MagicMock(side_effect=[2, 3])
        monkeypatch.setattr(cli_acp.os, "write", os_write)

        writer._write_all(b"abcde")

        assert [bytes(call.args[1]) for call in os_write.call_args_list] == [b"abcde", b"cde"]

    def test_threaded_writer_rejects_no_progress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = MagicMock()
        stream.fileno.return_value = 123
        writer = cli_acp._ThreadedPipeWriter(stream)
        monkeypatch.setattr(cli_acp.os, "write", MagicMock(return_value=0))

        with pytest.raises(OSError, match="made no progress"):
            writer._write_all(b"data")

    @pytest.mark.asyncio
    async def test_threaded_reader_pauses_descriptor_reads_until_consumer_drains(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reader = asyncio.StreamReader(limit=4)
        transport = cli_acp._ThreadedReadTransport()
        reader.set_transport(transport)
        to_thread = AsyncMock(side_effect=[b"123456789", b""])
        monkeypatch.setattr(cli_acp.asyncio, "to_thread", to_thread)

        pump = asyncio.create_task(cli_acp._pump_threaded_reader(123, reader, transport))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert to_thread.await_count == 1
        assert transport.is_reading() is False
        assert await reader.read(9) == b"123456789"
        await asyncio.wait_for(pump, timeout=1)
        assert to_thread.await_count == 2

    @pytest.mark.asyncio
    async def test_threaded_reader_surfaces_descriptor_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reader = asyncio.StreamReader()
        transport = cli_acp._ThreadedReadTransport()
        reader.set_transport(transport)
        monkeypatch.setattr(cli_acp.asyncio, "to_thread", AsyncMock(side_effect=OSError("closed")))

        await cli_acp._pump_threaded_reader(123, reader, transport)

        with pytest.raises(OSError, match="closed"):
            await reader.read()

    @pytest.mark.asyncio
    async def test_stdio_streams_refuses_unavailable_standard_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli_acp.sys, "stdin", None)

        with pytest.raises(RuntimeError, match="standard streams are unavailable"):
            await cli_acp._stdio_streams(stdout=MagicMock())


class TestStandaloneServices:
    def test_uses_registry_provider_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = MagicMock()
        cfg.hooks = {}
        cfg.agent.bot_name = "Kiro Crew"
        factory = object()
        sessions = object()
        memory = MagicMock()

        monkeypatch.setattr(cli_acp, "build_provider_factory", lambda value: factory)
        monkeypatch.setattr(cli_acp, "MemoryStore", lambda: memory)
        monkeypatch.setattr(cli_acp, "ContextBuilder", MagicMock(return_value=object()))
        monkeypatch.setattr(cli_acp, "HookManager", MagicMock(return_value=object()))
        monkeypatch.setattr(cli_acp.HooksConfig, "from_dict", lambda value: object())
        monkeypatch.setattr(cli_acp, "SkillsLoader", MagicMock(return_value=object()))
        monkeypatch.setattr(cli_acp, "LessonStore", MagicMock(return_value=object()))

        def make_sessions(value, *, provider_factory):
            assert value is cfg
            assert provider_factory is factory
            return sessions

        monkeypatch.setattr(cli_acp, "SessionManager", make_sessions)

        services = cli_acp._build_services(cfg)

        memory.init.assert_called_once_with()
        assert services.sessions is sessions


class _StubSessions:
    def __init__(self, raise_on_close: bool = False) -> None:
        self.closed = 0
        self._raise = raise_on_close

    async def close_all(self) -> None:
        self.closed += 1
        if self._raise:
            raise RuntimeError("close blew up")


class TestServeLifecycle:
    """`_serve --standalone` owns process teardown: kiro-cli children must not be orphaned.

    (The default gateway-proxy path spawns no local agent — the gateway reaps its
    own children — so teardown there closes the HTTP backend instead; see
    test_acp_server_http_backend.)
    """

    @staticmethod
    def _patch(monkeypatch: pytest.MonkeyPatch, sessions: _StubSessions) -> cli_acp._Services:
        reader = asyncio.StreamReader()
        reader.feed_eof()  # empty stdin -> serve() returns immediately

        class _W:
            def write(self, _data: bytes) -> None:
                return None

            async def drain(self) -> None:
                return None

        async def fake_streams() -> tuple[asyncio.StreamReader, object]:
            return reader, _W()

        monkeypatch.setattr(cli_acp, "_stdio_streams", fake_streams)
        monkeypatch.setattr(cli_acp, "warm_sel_singleton", AsyncMock())
        return cli_acp._Services(
            sessions=sessions,
            context_builder=object(),
            script_hooks=object(),
        )

    @pytest.mark.asyncio
    async def test_sessions_closed_on_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions = _StubSessions()
        services = self._patch(monkeypatch, sessions)
        await cli_acp._serve(
            argparse.Namespace(agent=None, verbose=False, standalone=True),
            standalone_services=services,
        )
        assert sessions.closed == 1

    @pytest.mark.asyncio
    async def test_standalone_warms_sel_before_stdio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions = _StubSessions()
        services = self._patch(monkeypatch, sessions)
        fake_streams = cli_acp._stdio_streams
        warmed = False

        async def warm() -> None:
            nonlocal warmed
            warmed = True

        async def checked_streams() -> tuple[asyncio.StreamReader, object]:
            assert warmed is True
            return await fake_streams()

        monkeypatch.setattr(cli_acp, "warm_sel_singleton", warm)
        monkeypatch.setattr(cli_acp, "_stdio_streams", checked_streams)

        await cli_acp._serve(
            argparse.Namespace(agent=None, verbose=False, standalone=True),
            standalone_services=services,
        )

        assert sessions.closed == 1

    @pytest.mark.asyncio
    async def test_close_failure_does_not_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Teardown is best-effort: a failing close_all must not turn a clean
        # editor disconnect into a crash.
        sessions = _StubSessions(raise_on_close=True)
        services = self._patch(monkeypatch, sessions)
        await cli_acp._serve(
            argparse.Namespace(agent=None, verbose=False, standalone=True),
            standalone_services=services,
        )
        assert sessions.closed == 1

    @pytest.mark.asyncio
    async def test_sigterm_cancels_serve_and_awaits_cleanup(self) -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()
        never = asyncio.Event()

        class Loop:
            callback = None
            removed: list[signal.Signals] = []

            def add_signal_handler(self, sig, callback) -> None:
                assert sig == signal.SIGTERM
                self.callback = callback

            def remove_signal_handler(self, sig) -> bool:
                self.removed.append(sig)
                return True

        async def serve() -> None:
            started.set()
            try:
                await never.wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        loop = Loop()
        task = asyncio.create_task(cli_acp._serve_until_terminated(serve(), loop=loop))
        await started.wait()
        assert loop.callback is not None
        loop.callback()
        await asyncio.wait_for(task, timeout=1)

        assert cleaned.is_set()
        assert loop.removed == [signal.SIGTERM]


class TestGatewayProxyAuthentication:
    def test_remote_token_is_consumed_before_backend_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        class StopAfterOpen(Exception):
            pass

        class Backend:
            def __init__(
                self, base_url: str, *, agent: str | None = None, token: str | None = None
            ) -> None:
                seen.update(base_url=base_url, agent=agent, token=token)

            async def open(self) -> None:
                seen["environment_after_construction"] = os.environ.get("KIROCREW_GATEWAY_TOKEN")
                raise StopAfterOpen

        cfg = MagicMock()
        cfg.agent.default_agent = "kirocrew"
        monkeypatch.setattr(cli_acp.KiroCrewConfig, "load", lambda: cfg)
        monkeypatch.setattr(cli_acp, "HttpGatewayBackend", Backend)
        monkeypatch.setenv("KIROCREW_GATEWAY_TOKEN", "presigned-token")

        with pytest.raises(StopAfterOpen):
            cli_acp.run_acp(
                argparse.Namespace(
                    agent=None,
                    gateway_url="https://gateway.example",
                    standalone=False,
                    verbose=False,
                )
            )

        assert seen == {
            "base_url": "https://gateway.example",
            "agent": "kirocrew",
            "token": "presigned-token",
            "environment_after_construction": None,
        }

    def test_gateway_config_loads_before_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def load() -> object:
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop()
            seen["loaded_without_event_loop"] = True
            cfg = MagicMock()
            cfg.agent.default_agent = "kirocrew"
            return cfg

        async def serve(_args: argparse.Namespace, **kwargs: object) -> None:
            seen["gateway_agent"] = kwargs.get("gateway_agent")

        monkeypatch.setattr(cli_acp.KiroCrewConfig, "load", load)
        monkeypatch.setattr(cli_acp, "_serve", serve)

        cli_acp.run_acp(argparse.Namespace(standalone=False, verbose=False, agent=None))

        assert seen == {
            "loaded_without_event_loop": True,
            "gateway_agent": "kirocrew",
        }

    def test_standalone_consumes_remote_token_before_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        class StopDuringConfig(Exception):
            pass

        def load() -> object:
            seen["environment_during_config"] = os.environ.get("KIROCREW_GATEWAY_TOKEN")
            raise StopDuringConfig

        monkeypatch.setattr(cli_acp.KiroCrewConfig, "load", load)
        monkeypatch.setenv("KIROCREW_GATEWAY_TOKEN", "presigned-token")

        with pytest.raises(StopDuringConfig):
            cli_acp.run_acp(argparse.Namespace(standalone=True, verbose=False))

        assert seen == {"environment_during_config": None}
