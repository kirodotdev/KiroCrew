from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from kiro_byo.native_tools import NativeToolRuntime
from kiro_byo.process_manager import ByteRingBuffer, ProcessError, ProcessManager


def _py(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


def _wait_for_text(manager: ProcessManager, process_id: str, needle: str, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = manager.snapshot(process_id)
        if needle in (last.get("stdout") or "") or needle in (last.get("stderr") or ""):
            return last
        time.sleep(0.03)
    raise AssertionError(f"missing {needle!r}; snapshot={last!r}")


def test_ring_buffer_is_bounded_and_marks_truncation() -> None:
    buf = ByteRingBuffer(4096)
    buf.append(b"a" * 5000)
    text = buf.tail_text(5000)
    assert text.startswith("[older output truncated]\n")
    assert text.endswith("a" * 4096)
    assert buf.total_bytes == 5000
    assert buf.truncated_bytes == 904


def test_long_running_process_captures_output_and_terminates_tree() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manager = ProcessManager(max_processes=2, buffer_bytes=8192)
        try:
            handle = manager.start(
                _py("import time; print('ready', flush=True); time.sleep(30)"),
                command="python long runner",
                cwd=Path(tmp),
                initial_wait_ms=20,
            )
            snap = _wait_for_text(manager, handle.process_id, "ready")
            assert snap["state"] == "running"
            assert snap["pid"] > 0
            stopped = manager.terminate(handle.process_id, grace_seconds=0.2)
            assert stopped.state in {"terminated", "killed", "exited"}
            assert stopped.proc.poll() is not None
        finally:
            manager.close()


def test_process_stdin_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manager = ProcessManager(buffer_bytes=8192)
        try:
            handle = manager.start(
                _py("import sys; line=sys.stdin.readline(); print('echo:'+line.strip(), flush=True)"),
                command="python stdin echo",
                cwd=Path(tmp),
                initial_wait_ms=20,
            )
            manager.send_stdin(handle.process_id, "hello", append_newline=True)
            snap = _wait_for_text(manager, handle.process_id, "echo:hello")
            deadline = time.monotonic() + 2
            while snap["state"] == "running" and time.monotonic() < deadline:
                time.sleep(0.02)
                snap = manager.snapshot(handle.process_id)
            assert "echo:hello" in snap["stdout"]
            assert snap["state"] == "exited"
            assert snap["exit_code"] == 0
        finally:
            manager.close()


def test_concurrent_process_limit_is_enforced() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manager = ProcessManager(max_processes=1)
        try:
            first = manager.start(
                _py("import time; time.sleep(30)"),
                command="first",
                cwd=Path(tmp),
                initial_wait_ms=0,
            )
            with pytest.raises(ProcessError, match="maximum concurrent"):
                manager.start(
                    _py("import time; time.sleep(30)"),
                    command="second",
                    cwd=Path(tmp),
                    initial_wait_ms=0,
                )
            manager.terminate(first.process_id, force=True)
        finally:
            manager.close()


def test_completed_process_history_is_bounded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manager = ProcessManager(max_history=4, buffer_bytes=4096)
        try:
            process_ids = []
            for index in range(7):
                handle = manager.start(
                    _py(f"print('done-{index}')"),
                    command=f"done-{index}",
                    cwd=Path(tmp),
                    initial_wait_ms=0,
                )
                process_ids.append(handle.process_id)
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                snapshots = manager.list()
                if snapshots and all(snap["state"] != "running" for snap in snapshots):
                    break
                time.sleep(0.05)
            snapshots = manager.list()
            assert len(snapshots) <= 4
            assert all(snap["state"] != "running" for snap in snapshots)
            assert any("done-6" in snap["stdout"] for snap in snapshots)
        finally:
            manager.close()


def test_close_terminates_all_running_processes_and_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manager = ProcessManager(max_processes=2)
        a = manager.start(_py("import time; time.sleep(30)"), command="a", cwd=Path(tmp), initial_wait_ms=0)
        b = manager.start(_py("import time; time.sleep(30)"), command="b", cwd=Path(tmp), initial_wait_ms=0)
        manager.close()
        assert a.proc.poll() is not None
        assert b.proc.poll() is not None
        manager.close()



def test_native_process_tools_wrap_platform_shell_and_control_process() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = NativeToolRuntime(root)
        command = (
            "Write-Output process-ready; Start-Sleep -Seconds 30"
            if os.name == "nt"
            else "printf 'process-ready\n'; sleep 30"
        )
        try:
            invocation = runtime.validate(
                "execute_shell_process",
                {"command": command, "cwd": ".", "initial_wait_ms": 2000},
            )
            started = json.loads(runtime.execute(invocation).text)
            assert started["process_id"].startswith("proc_")
            assert started["pid"] > 0
            assert "process-ready" in started["stdout"]

            control = runtime.validate(
                "control_shell_process",
                {"process_id": started["process_id"], "action": "terminate", "grace_seconds": 0.2},
            )
            stopped = json.loads(runtime.execute(control).text)
            assert stopped["state"] in {"terminated", "killed", "exited"}
        finally:
            runtime.close()

def test_unknown_process_id_fails_closed() -> None:
    manager = ProcessManager()
    try:
        with pytest.raises(ProcessError, match="unknown process_id"):
            manager.get("proc_missing")
    finally:
        manager.close()
