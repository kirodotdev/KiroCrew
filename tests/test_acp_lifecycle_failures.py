"""Regressions for ACP registry ownership and stdout backpressure.

No provider network is used. Event-controlled fake runtimes force the exact
interleavings that a normal unit test would rarely encounter.
"""
from __future__ import annotations

import json
import threading
import time
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from kiro_byo.acp_server import (
    AcpServer,
    OutboundTransportError,
    Session,
    SessionBusyError,
)


class FakeRuntime:
    def __init__(self):
        self.closed = threading.Event()
        self.close_count = 0

    def close(self):
        self.close_count += 1
        self.closed.set()


class HoldingOutput:
    def __init__(self):
        self.writing = threading.Event()
        self.release = threading.Event()
        self.lines = []

    def write(self, line):
        self.writing.set()
        if not self.release.wait(3):
            raise OSError("test writer timeout")
        self.lines.append(line)
        return len(line)

    def flush(self):
        pass


class BrokenOutput:
    def write(self, _line):
        raise BrokenPipeError("peer disconnected")

    def flush(self):
        pass


def _server(out=None):
    return AcpServer(stdin=StringIO(), stdout=out or StringIO())


def test_replacement_refuses_active_turn_without_cancelling_or_closing():
    server = _server()
    old_runtime, new_runtime = FakeRuntime(), FakeRuntime()
    old = Session("busy", "/tmp", "model", native=old_runtime)
    server._put_session(old)
    assert old.turn_lock.acquire(blocking=False)
    try:
        with pytest.raises(SessionBusyError, match="active turn"):
            server._put_session(Session("busy", "/tmp", "new", native=new_runtime))
        assert server._get_session("busy") is old
        assert not old.cancel_event.is_set()
        assert not old_runtime.closed.is_set()
        assert not new_runtime.closed.is_set()
    finally:
        old.turn_lock.release()
        server._executor.shutdown(wait=True)


def test_session_restore_refuses_busy_session_before_touching_persistence():
    server = _server()
    old = Session("busy", "/tmp", "model")
    server._put_session(old)
    old.turn_lock.acquire()
    try:
        with patch.object(server, "_load_persisted_record", side_effect=AssertionError("must not read")):
            with pytest.raises(SessionBusyError):
                server._restore_session("busy", {}, replay=False)
        assert server._get_session("busy") is old
    finally:
        old.turn_lock.release()
        server._executor.shutdown(wait=True)


def test_transition_rejects_prompt_and_concurrent_restore():
    server = _server()
    old = Session("stable", "/tmp", "model")
    server._put_session(old)
    entered, release = threading.Event(), threading.Event()
    errors = []

    def paused_restore(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        raise KeyError("not found")

    def restore_worker():
        try:
            server._restore_session("stable", {}, replay=True)
        except KeyError:
            pass
        except Exception as exc:
            errors.append(exc)

    try:
        with patch.object(server, "_restore_session_impl", side_effect=paused_restore):
            worker = threading.Thread(target=restore_worker)
            worker.start()
            assert entered.wait(2)
            with pytest.raises(SessionBusyError):
                server._restore_session("stable", {}, replay=True)
            out = server.stdout
            server._handle_prompt_request(9, {"sessionId": "stable", "prompt": [{"type": "text", "text": "hi"}]})
            response = json.loads(out.getvalue().splitlines()[-1])
            assert response["error"]["code"] == -32002
            assert not old.active_turn
            release.set()
            worker.join(3)
            assert not worker.is_alive()
            assert not errors
            assert not old.replacing
    finally:
        release.set()
        server._executor.shutdown(wait=True)


def test_retirement_defers_close_until_turn_owner_exits():
    server = _server()
    runtime = FakeRuntime()
    old = Session("retired", "/tmp", "model", native=runtime)
    server._put_session(old)
    old.turn_lock.acquire()
    try:
        popped = server._pop_session("retired")
        assert popped is old
        assert not server._quiesce_and_close_runtime(old, timeout=0.01)
        assert not runtime.closed.is_set()
        with pytest.raises(SessionBusyError):
            server._put_session(Session("retired", "/tmp", "replacement"))
        with pytest.raises(SessionBusyError):
            server._restore_session("retired", {}, replay=False)
    finally:
        old.turn_lock.release()
    try:
        assert runtime.closed.wait(2)
        assert runtime.close_count == 1
        deadline = time.monotonic() + 2
        while "retired" in server._retiring_sessions and time.monotonic() < deadline:
            time.sleep(0.005)
        assert "retired" not in server._retiring_sessions
    finally:
        server._executor.shutdown(wait=True)


def test_writer_broken_pipe_is_terminal_not_silent_draining():
    server = _server(BrokenOutput())
    server._start_outbound_writer()
    try:
        server.send({"jsonrpc": "2.0", "id": 1, "result": {}})
        assert server._outbound_failed.wait(2)
        with pytest.raises(OutboundTransportError):
            server.send({"jsonrpc": "2.0", "id": 2, "result": {}})
        server._writer_thread.join(timeout=2)
        assert not server._writer_thread.is_alive()
    finally:
        server._executor.shutdown(wait=True)


def test_stalled_stdout_has_bounded_nonblocking_queue():
    output = HoldingOutput()
    server = _server(output)
    server._start_outbound_writer()
    try:
        server.send({"jsonrpc": "2.0", "id": 0, "result": {}})
        assert output.writing.wait(2)
        before = time.monotonic()
        with pytest.raises(OutboundTransportError, match="queue exceeded"):
            for n in range(2055):
                server.send({"jsonrpc": "2.0", "id": n + 1, "result": {}})
        assert time.monotonic() - before < 5
        assert server._outbound.qsize() <= 2048
        assert server._outbound_failed.is_set()
    finally:
        output.release.set()
        server._writer_thread.join(3)
        server._executor.shutdown(wait=True)


def test_pre_run_send_finishes_before_writer_activation():
    output = HoldingOutput()
    server = _server(output)
    sender = threading.Thread(target=lambda: server.send({"jsonrpc": "2.0", "id": "early"}))
    sender.start()
    try:
        assert output.writing.wait(2)
        starter = threading.Thread(target=server._start_outbound_writer)
        starter.start()
        time.sleep(0.02)
        assert not server._writer_started  # start cannot overtake inline write
        output.release.set()
        sender.join(2)
        starter.join(2)
        assert server._writer_started
        server.send({"jsonrpc": "2.0", "id": "late"})
        deadline = time.monotonic() + 2
        while len(output.lines) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert [json.loads(line)["id"] for line in output.lines] == ["early", "late"]
    finally:
        output.release.set()
        server._executor.shutdown(wait=True)


def test_outbound_failure_cleans_up_pending_permission_request():
    server = _server(BrokenOutput())
    server._start_outbound_writer()
    try:
        server.send({"jsonrpc": "2.0", "id": "start"})
        assert server._outbound_failed.wait(2)
        with pytest.raises(OutboundTransportError):
            server._request_client("session/request_permission", {})
        assert not server._pending_client_requests
    finally:
        server._executor.shutdown(wait=True)


def test_delete_does_not_remove_session_record_while_turn_is_running():
    with TemporaryDirectory() as tmp:
        with patch.dict("os.environ", {"KIRO_BYO_HOME": tmp}):
            server = _server()
            old = Session("delete-me", str(Path(tmp).resolve()), "model")
            server._put_session(old)
            server._persist_session(old)
            assert server._session_path("delete-me").exists()
            old.turn_lock.acquire()
            original_quiesce = server._quiesce_and_close_runtime
            try:
                with patch.object(
                    server,
                    "_quiesce_and_close_runtime",
                    side_effect=lambda sess, **kw: original_quiesce(sess, timeout=0.01, **kw),
                ):
                    server._handle_session_delete_request(14, {"sessionId": "delete-me"})
                assert server._session_path("delete-me").exists()
                assert "delete-me" in server._retiring_sessions
                with pytest.raises(SessionBusyError):
                    server._put_session(Session("delete-me", str(Path(tmp).resolve()), "model"))
            finally:
                old.turn_lock.release()
            try:
                deadline = time.monotonic() + 2
                while server._session_path("delete-me").exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                assert not server._session_path("delete-me").exists()
                assert "delete-me" not in server._retiring_sessions
            finally:
                server._executor.shutdown(wait=True)
