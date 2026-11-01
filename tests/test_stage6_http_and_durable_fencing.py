"""Stage 6: HTTP MCP cancellation fences and strict durable ledger IDs."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.mcp_transports import HttpMcpServer, McpTransportError


def test_http_close_wakes_inflight_call_and_rejects_late_reply() -> None:
    received = threading.Event()
    complete = threading.Event()
    results: list[object] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get('Content-Length', 0))
            payload = json.loads(self.rfile.read(length))
            received.set()
            complete.wait(timeout=4.0)
            body = json.dumps({'jsonrpc': '2.0', 'id': payload.get('id'),
                               'result': {'resultType': 'complete', 'content': [{'type': 'text', 'text': 'too late'}]}}).encode()
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    listener = threading.Thread(target=server.serve_forever, daemon=True)
    listener.start()
    client = HttpMcpServer('local', f'http://127.0.0.1:{server.server_port}/mcp', {})
    client._initialized = True
    client._era = 'modern'

    def execute():
        try:
            results.append(client.call('delay', {}))
        except BaseException as exc:
            results.append(exc)

    worker = threading.Thread(target=execute, daemon=True)
    try:
        worker.start()
        assert received.wait(timeout=2.0)
        t0 = time.monotonic()
        client.close()
        worker.join(timeout=1.0)
        assert not worker.is_alive(), 'closing HTTP MCP left caller blocked on 120-second read'
        assert time.monotonic() - t0 < 1.2
        assert len(results) == 1 and isinstance(results[0], McpTransportError), results
        assert 'closed' in str(results[0]).lower()
        complete.set()
        time.sleep(.1)  # delayed network response must not be delivered to the closed call
        assert len(results) == 1
    finally:
        complete.set()
        client.close()
        server.shutdown()
        server.server_close()


def test_http_close_is_terminal_and_does_not_make_more_requests() -> None:
    client = HttpMcpServer('closed', 'https://example.invalid/mcp', {})
    client.close()
    with patch('kiro_byo.mcp_transports.urllib.request.urlopen') as urlopen:
        with pytest.raises(McpTransportError, match='closed'):
            client.call('must-not-dispatch', {})
        urlopen.assert_not_called()


def test_durable_unknown_step_and_attempt_not_silently_acknowledged(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / 'runs')
    run = store.create_run('s')
    step = store.start_step(run['run_id'], kind='turn')
    assert step
    attempt = store.start_attempt(run['run_id'], step['step_id'])
    assert attempt
    revision = store.get(run['run_id'])['revision']
    with pytest.raises(ValueError, match='unknown'):
        store.finish_attempt(run['run_id'], step['step_id'], 'not-a-real-attempt', status='completed')
    with pytest.raises(ValueError, match='unknown'):
        store.finish_step(run['run_id'], 'not-a-real-step', status='completed')
    assert store.get(run['run_id'])['revision'] == revision
    assert store.get(run['run_id'])['steps'][0]['attempts'][0]['status'] == 'running'


def test_http_pending_request_limit_is_bounded_and_close_wakes_all() -> None:
    gate = threading.Event()
    entered = threading.Event()
    seen: list[object] = []
    client = HttpMcpServer('bounded', 'https://example.invalid/mcp', {})
    worker_count = 0
    counter_lock = threading.Lock()

    def blocked_network(*_args, **_kwargs):
        nonlocal worker_count
        with counter_lock:
            worker_count += 1
            if worker_count == 8:
                entered.set()
        gate.wait(timeout=5)
        return {'ok': True}

    def run_call():
        try:
            client._request('tools/call', {}, timeout=120)
        except BaseException as exc:
            seen.append(exc)

    with patch.object(client, '_post_blocking', side_effect=blocked_network):
        calls = [threading.Thread(target=run_call, daemon=True) for _ in range(8)]
        try:
            for call in calls:
                call.start()
            assert entered.wait(timeout=2)
            with pytest.raises(McpTransportError, match='too many pending'):
                client._request('tools/call', {}, timeout=120)
            with counter_lock:
                assert worker_count == 8
            client.close()
            for call in calls:
                call.join(timeout=1)
                assert not call.is_alive()
            assert len(seen) == 8
            assert all(isinstance(item, McpTransportError) for item in seen)
        finally:
            client.close()
            gate.set()
