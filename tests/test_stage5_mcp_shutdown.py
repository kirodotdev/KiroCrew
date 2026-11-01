"""Stage 5: closing an MCP session must fence and wake in-flight calls."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch
import sys
import tempfile
import textwrap
import threading
import time

import pytest

from kiro_byo.mcp_client import McpManager
from kiro_byo.mcp_transports import McpError, McpTransportError, StdioMcpServer


def _hung_stdio(tmp_path: Path) -> tuple[StdioMcpServer, Path]:
    started = tmp_path / 'tool_started'
    script = tmp_path / 'mcp_hang.py'
    script.write_text(textwrap.dedent('''
        import json, pathlib, sys, time
        flag = pathlib.Path(sys.argv[1])
        for line in sys.stdin:
            req = json.loads(line)
            method = req.get('method')
            rid = req.get('id')
            if method == 'server/discover':
                result = {'resultType':'complete', 'supportedVersions':['2026-07-28'],
                          'capabilities':{'tools':{}}, 'ttlMs':0, 'cacheScope':'public'}
            elif method == 'tools/list':
                result = {'resultType':'complete','tools':[{'name':'slow',
                          'inputSchema':{'type':'object'}}], 'ttlMs':0,'cacheScope':'private'}
            elif method == 'tools/call':
                flag.write_text('started', encoding='utf-8')
                time.sleep(240)
                continue
            else:
                continue
            print(json.dumps({'jsonrpc':'2.0', 'id':rid, 'result':result}), flush=True)
    '''), encoding='utf-8')
    return StdioMcpServer('hung', sys.executable, [str(script), str(started)], {}), started


def _await_file(path: Path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    pytest.fail('MCP server did not receive the tool request')


def test_closing_stdio_wakes_inflight_request_and_fails_closed(tmp_path: Path) -> None:
    server, started = _hung_stdio(tmp_path)
    server.start()
    errors: list[BaseException] = []
    thread = threading.Thread(target=lambda: _invoke(server, errors), daemon=True)
    thread.start()
    try:
        _await_file(started)
        server.close()
        thread.join(timeout=1.5)
        assert not thread.is_alive(), 'closing MCP session left a 120-second pending call'
        assert len(errors) == 1 and isinstance(errors[0], McpTransportError), errors
    finally:
        server.close()


def _invoke(server: StdioMcpServer, errors: list[BaseException]) -> None:
    try:
        server.call('slow', {})
    except BaseException as exc:
        errors.append(exc)


def test_mcp_manager_rejects_calls_after_close_without_waiting_for_discovery() -> None:
    manager = McpManager([])
    manager.close()
    start = time.monotonic()
    result = manager.call('mcp__nonexistent__tool', {})
    assert result.startswith('Error:')
    assert 'closed' in result.lower()
    assert time.monotonic() - start < 0.5


def test_mcp_stdio_process_uses_isolated_process_group() -> None:
    with tempfile.TemporaryDirectory() as temp:
        server, _flag = _hung_stdio(Path(temp))
        server.start()
        try:
            assert server.process is not None
            if os.name == 'posix':
                assert os.getpgid(server.process.pid) == server.process.pid
            elif os.name == 'nt':
                # On Windows, the process group is not introspectable using
                # stdlib; Popen configuration is checked via the launch path.
                pass
        finally:
            server.close()


def test_peer_exit_wakes_request_without_waiting_for_tool_deadline(tmp_path: Path) -> None:
    server, started = _hung_stdio(tmp_path)
    server.start()
    errors: list[BaseException] = []
    thread = threading.Thread(target=lambda: _invoke(server, errors), daemon=True)
    thread.start()
    try:
        _await_file(started)
        assert server.process is not None
        server.process.kill()  # peer exits without returning JSON-RPC
        server.process.wait(timeout=2)
        thread.join(timeout=1.5)
        assert not thread.is_alive(), 'stdio EOF failed to wake an MCP request'
        assert len(errors) == 1 and isinstance(errors[0], McpTransportError), errors
    finally:
        server.close()


@pytest.mark.skipif(os.name != 'posix', reason='uses POSIX process groups')
def test_stdio_close_kills_owned_process_group(tmp_path: Path) -> None:
    server, _started = _hung_stdio(tmp_path)
    server.start()
    try:
        assert server.process is not None
        group_id = server.process.pid
        with patch('kiro_byo.mcp_transports.os.killpg', wraps=os.killpg) as killpg:
            server.close()
        killpg.assert_any_call(group_id, __import__('signal').SIGKILL)
    finally:
        server.close()
