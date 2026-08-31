"""``kirocrew acp`` entrypoint: registration, jail scope, stdout discipline."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys

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
            with (
                os.fdopen(in_r, "rb", buffering=0) as inf,
                os.fdopen(w_fd, "wb", buffering=0) as outf,
            ):
                _reader, writer = await cli_acp._stdio_streams(stdin=inf, stdout=outf)
                writer.write(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
                await writer.drain()
                with os.fdopen(r_fd, "rb", buffering=0) as back:
                    echoed = await asyncio.get_running_loop().run_in_executor(None, back.readline)
            assert json.loads(echoed)["id"] == 1
        finally:
            for fd in (in_w,):
                with contextlib.suppress(OSError):
                    os.close(fd)


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
    def _patch(monkeypatch: pytest.MonkeyPatch, sessions: _StubSessions) -> None:
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
        monkeypatch.setattr(
            cli_acp,
            "_build_services",
            lambda _cfg: cli_acp._Services(sessions=sessions, context_builder=object()),
        )

    @pytest.mark.asyncio
    async def test_sessions_closed_on_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions = _StubSessions()
        self._patch(monkeypatch, sessions)
        await cli_acp._serve(argparse.Namespace(agent=None, verbose=False, standalone=True))
        assert sessions.closed == 1

    @pytest.mark.asyncio
    async def test_close_failure_does_not_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Teardown is best-effort: a failing close_all must not turn a clean
        # editor disconnect into a crash.
        sessions = _StubSessions(raise_on_close=True)
        self._patch(monkeypatch, sessions)
        await cli_acp._serve(argparse.Namespace(agent=None, verbose=False, standalone=True))
        assert sessions.closed == 1
