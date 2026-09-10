"""Tests for the standalone ACP MCP stdio relay."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from tmpdir_helpers import short_tmp_base

from kiro_crew import platform_compat
from kiro_crew.acp_server import mcp_proxy


@pytest.mark.asyncio
async def test_pump_copies_chunks_and_closes_writer() -> None:
    read = AsyncMock(side_effect=[b"one", b"two", b""])
    write = MagicMock()
    drain = AsyncMock()
    close = MagicMock()

    await mcp_proxy._pump(read, write, drain, close)

    assert write.call_args_list == [((b"one",),), ((b"two",),)]
    assert drain.await_count == 2
    close.assert_called_once_with()


@pytest.mark.asyncio
async def test_pump_swallows_connection_and_close_errors() -> None:
    read = AsyncMock(side_effect=ConnectionError("closed"))
    close = MagicMock(side_effect=OSError("closed"))

    await mcp_proxy._pump(read, MagicMock(), AsyncMock(), close)

    close.assert_called_once_with()


@pytest.mark.asyncio
async def test_run_reports_connection_failure(monkeypatch, capsys) -> None:
    connect = AsyncMock(side_effect=OSError("offline"))
    monkeypatch.setattr(mcp_proxy.asyncio, "open_unix_connection", connect, raising=False)

    assert await mcp_proxy._run("missing.sock", "secret") == 1
    assert "cannot connect" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_run_closes_socket_after_auth_failure(monkeypatch, capsys) -> None:
    reader = MagicMock()
    writer = MagicMock()
    writer.drain = AsyncMock(side_effect=ConnectionError("closed"))
    connect = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(mcp_proxy.asyncio, "open_unix_connection", connect, raising=False)

    assert await mcp_proxy._run("proxy.sock", "secret") == 1
    writer.write.assert_called_once_with(b"secret\n")
    writer.close.assert_called_once_with()
    assert "auth write failed" in capsys.readouterr().err


@pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="requires Unix sockets and pipe transports"
)
@pytest.mark.asyncio
async def test_run_relays_supervisor_output_to_stdout(monkeypatch) -> None:
    socket_root = tempfile.mkdtemp(prefix="acp-proxy-", dir=short_tmp_base())
    socket_path = os.path.join(socket_root, "m.sock")
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    authenticated: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        authenticated.set_result(await reader.readline())
        writer.write(b'{"jsonrpc":"2.0","id":1}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=socket_path)
    try:
        with (
            os.fdopen(in_r, "r", buffering=1) as stdin,
            os.fdopen(out_w, "w", buffering=1) as stdout,
        ):
            monkeypatch.setattr(mcp_proxy.sys, "stdin", stdin)
            monkeypatch.setattr(mcp_proxy.sys, "stdout", stdout)
            assert await asyncio.wait_for(mcp_proxy._run(socket_path, "secret"), timeout=3) == 0
        assert await asyncio.wait_for(authenticated, timeout=3) == b"secret\n"
        assert await asyncio.wait_for(asyncio.to_thread(os.read, out_r, 4096), timeout=3) == (
            b'{"jsonrpc":"2.0","id":1}\n'
        )
    finally:
        server.close()
        await server.wait_closed()
        for fd in (in_w, out_r):
            with contextlib.suppress(OSError):
                os.close(fd)
        shutil.rmtree(socket_root)


def test_read_token_uses_named_file(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "proxy-token"
    token_file.write_text(" secret\n", encoding="utf-8")
    monkeypatch.setenv(mcp_proxy._TOKEN_FILE_ENV, str(token_file))

    assert mcp_proxy._read_token() == "secret"


def test_read_token_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(mcp_proxy._TOKEN_FILE_ENV, raising=False)
    assert mcp_proxy._read_token() == ""

    monkeypatch.setenv(mcp_proxy._TOKEN_FILE_ENV, str(tmp_path / "missing"))
    assert mcp_proxy._read_token() == ""


def test_main_requires_socket(monkeypatch, capsys) -> None:
    monkeypatch.delenv(mcp_proxy._SOCKET_ENV, raising=False)

    assert mcp_proxy.main([]) == 2
    assert "no --socket" in capsys.readouterr().err


def test_main_runs_proxy_with_file_token(monkeypatch) -> None:
    marker = object()
    run = MagicMock(return_value=marker)
    asyncio_run = MagicMock(return_value=7)
    monkeypatch.setattr(mcp_proxy, "_read_token", lambda: "secret")
    monkeypatch.setattr(mcp_proxy, "_run", run)
    monkeypatch.setattr(mcp_proxy.asyncio, "run", asyncio_run)

    assert mcp_proxy.main(["--socket", "proxy.sock"]) == 7
    run.assert_called_once_with("proxy.sock", "secret")
    asyncio_run.assert_called_once_with(marker)


def test_main_treats_keyboard_interrupt_as_clean_exit(monkeypatch) -> None:
    monkeypatch.setattr(mcp_proxy, "_run", lambda _path, _token: object())
    monkeypatch.setattr(mcp_proxy.asyncio, "run", MagicMock(side_effect=KeyboardInterrupt))

    assert mcp_proxy.main(["--socket", "proxy.sock"]) == 0
