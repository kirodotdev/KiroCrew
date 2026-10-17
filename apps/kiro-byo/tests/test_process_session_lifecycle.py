from __future__ import annotations

import os
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.native_tools import NativeToolRuntime


def _start_sleeping_process(runtime: NativeToolRuntime) -> object:
    return runtime._processes.start(  # structural ownership test: runtime owns this registry
        [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
        command="session-owned-sleeper",
        cwd=runtime.workspace_root,
        initial_wait_ms=0,
    )


def test_session_close_terminates_native_processes() -> None:
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as app_home:
        runtime = NativeToolRuntime(Path(tmp))
        handle = _start_sleeping_process(runtime)
        with patch.dict(os.environ, {"KIRO_BYO_HOME": app_home}):
            server = AcpServer(stdin=StringIO(), stdout=StringIO())
        try:
            with patch("kiro_byo.acp_server.home_dir", return_value=Path(app_home)):
                server._put_session(Session("s-process", str(Path(tmp).resolve()), "Model", native=runtime))
                server.handle({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session/close",
                    "params": {"sessionId": "s-process"},
                })
            assert handle.proc.poll() is not None
        finally:
            server._executor.shutdown(wait=True)
            runtime.close()


def test_replacing_session_runtime_closes_old_native_resources() -> None:
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as app_home:
        root = Path(tmp)
        old_runtime = NativeToolRuntime(root)
        new_runtime = NativeToolRuntime(root)
        handle = _start_sleeping_process(old_runtime)
        with patch.dict(os.environ, {"KIRO_BYO_HOME": app_home}):
            server = AcpServer(stdin=StringIO(), stdout=StringIO())
        try:
            with patch("kiro_byo.acp_server.home_dir", return_value=Path(app_home)):
                server._put_session(Session("same", str(root.resolve()), "Model", native=old_runtime))
                server._put_session(Session("same", str(root.resolve()), "Model", native=new_runtime))
            assert handle.proc.poll() is not None
        finally:
            popped = server._pop_session("same")
            if popped and popped.native:
                popped.native.close()
            server._executor.shutdown(wait=True)
            old_runtime.close()
