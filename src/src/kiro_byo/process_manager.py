"""Long-running shell-process management for Kiro BYO native tools.

A ProcessManager is intentionally session-local.  The owning NativeToolRuntime
creates one manager and closes it with the ACP session, which makes process
ownership a structural property instead of a second authorization database.
Crew remains authoritative for user approval; this module owns only actual OS
resources, bounded output buffering and cleanup.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ProcessError(RuntimeError):
    pass


class ByteRingBuffer:
    """Thread-safe bounded tail buffer for a process output stream."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(4096, int(max_bytes))
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.total_bytes = 0
        self.truncated_bytes = 0

    def append(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self.total_bytes += len(data)
            self._buf.extend(data)
            excess = len(self._buf) - self.max_bytes
            if excess > 0:
                del self._buf[:excess]
                self.truncated_bytes += excess

    def tail_text(self, max_chars: int) -> str:
        limit = max(256, int(max_chars))
        with self._lock:
            data = bytes(self._buf)
            dropped = self.truncated_bytes
        text = data.decode("utf-8", errors="replace")
        if len(text) > limit:
            text = text[-limit:]
            dropped_marker = True
        else:
            dropped_marker = dropped > 0
        if dropped_marker:
            return f"[older output truncated]\n{text}"
        return text


@dataclass
class ProcessHandle:
    process_id: str
    proc: subprocess.Popen[bytes]
    command: str
    cwd: Path
    created_at: float
    stdout: ByteRingBuffer
    stderr: ByteRingBuffer
    state: str = "running"
    exit_code: int | None = None
    ended_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _readers: list[threading.Thread] = field(default_factory=list, repr=False)

    @property
    def pid(self) -> int:
        return int(self.proc.pid)

    def refresh(self) -> None:
        code = self.proc.poll()
        if code is None:
            return
        with self._lock:
            self.exit_code = int(code)
            if self.state == "running":
                self.state = "exited"
            if self.ended_at is None:
                self.ended_at = time.time()


class ProcessManager:
    """Own long-running process groups for one native-tool runtime/session."""

    def __init__(
        self,
        *,
        max_processes: int = 8,
        max_history: int = 32,
        buffer_bytes: int = 1024 * 1024,
    ) -> None:
        self.max_processes = max(1, int(max_processes))
        self.max_history = max(4, int(max_history))
        self.buffer_bytes = max(4096, int(buffer_bytes))
        self._handles: dict[str, ProcessHandle] = {}
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _reader(stream: Any, buffer: ByteRingBuffer) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                buffer.append(chunk)
        except (OSError, ValueError):
            return
        finally:
            try:
                stream.close()
            except Exception:
                pass

    @staticmethod
    def _popen_kwargs(cwd: Path) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "cwd": cwd,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": dict(os.environ),
            "bufsize": 0,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        return kwargs


    @staticmethod
    def _dispose_handle(handle: ProcessHandle) -> None:
        # Close all three pipes first. A reader thread blocks on stdout/stderr
        # read(); closing the stream is what unblocks it so the thread can
        # exit. Closing only stdin (the old behaviour) could leave a reader
        # and its OS pipe FDs alive if the child's pipe was held open.
        for stream in (handle.proc.stdout, handle.proc.stderr, handle.proc.stdin):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        for thread in handle._readers:
            thread.join(timeout=0.5)

    def _prune_completed(self) -> None:
        completed: list[ProcessHandle] = []
        for handle in self._handles.values():
            handle.refresh()
            if handle.state != "running":
                completed.append(handle)
        completed.sort(key=lambda item: item.created_at, reverse=True)
        stale = completed[self.max_history :]
        for handle in stale:
            self._handles.pop(handle.process_id, None)
            self._dispose_handle(handle)

    def _live_count(self) -> int:
        count = 0
        for handle in self._handles.values():
            handle.refresh()
            if handle.state == "running":
                count += 1
        return count

    def start(self, argv: list[str], *, command: str, cwd: Path, initial_wait_ms: int = 120) -> ProcessHandle:
        with self._lock:
            if self._closed:
                raise ProcessError("process manager is closed")
            self._prune_completed()
            if self._live_count() >= self.max_processes:
                raise ProcessError(f"maximum concurrent long-running processes reached ({self.max_processes})")
            try:
                proc: subprocess.Popen[bytes] = subprocess.Popen(argv, **self._popen_kwargs(cwd))
            except OSError as exc:
                raise ProcessError(f"failed to start process: {exc}") from exc
            process_id = "proc_" + uuid.uuid4().hex[:16]
            handle = ProcessHandle(
                process_id=process_id,
                proc=proc,
                command=command,
                cwd=cwd,
                created_at=time.time(),
                stdout=ByteRingBuffer(self.buffer_bytes),
                stderr=ByteRingBuffer(self.buffer_bytes),
            )
            assert proc.stdout is not None and proc.stderr is not None
            for stream, target, label in (
                (proc.stdout, handle.stdout, "stdout"),
                (proc.stderr, handle.stderr, "stderr"),
            ):
                thread = threading.Thread(
                    target=self._reader,
                    args=(stream, target),
                    name=f"kiro-byo-{process_id}-{label}",
                    daemon=True,
                )
                handle._readers.append(thread)
                thread.start()
            self._handles[process_id] = handle
        if initial_wait_ms > 0:
            time.sleep(min(initial_wait_ms, 2000) / 1000.0)
        handle.refresh()
        with self._lock:
            self._prune_completed()
        return handle

    def get(self, process_id: str) -> ProcessHandle:
        with self._lock:
            handle = self._handles.get(process_id)
        if handle is None:
            raise ProcessError(f"unknown process_id: {process_id}")
        handle.refresh()
        return handle

    @staticmethod
    def _kill_tree(handle: ProcessHandle, *, force: bool) -> None:
        proc = handle.proc
        if proc.poll() is not None:
            return
        if os.name == "nt":
            command = ["taskkill", "/PID", str(proc.pid), "/T"]
            if force:
                command.append("/F")
            try:
                subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                if proc.poll() is not None:
                    return
            except Exception:
                pass
            try:
                if force:
                    proc.kill()
                else:
                    proc.terminate()
            except OSError:
                pass
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                if force:
                    proc.kill()
                else:
                    proc.terminate()
            except OSError:
                pass

    def send_stdin(self, process_id: str, data: str, *, append_newline: bool = False) -> ProcessHandle:
        handle = self.get(process_id)
        if handle.state != "running" or handle.proc.stdin is None:
            raise ProcessError(f"process is not accepting stdin: {process_id}")
        payload = data + ("\n" if append_newline else "")
        try:
            handle.proc.stdin.write(payload.encode("utf-8"))
            handle.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            handle.refresh()
            raise ProcessError(f"failed to write process stdin: {exc}") from exc
        return handle

    def terminate(self, process_id: str, *, force: bool = False, grace_seconds: float = 2.0) -> ProcessHandle:
        handle = self.get(process_id)
        if handle.state != "running":
            return handle
        self._kill_tree(handle, force=force)
        if not force:
            try:
                handle.proc.wait(timeout=max(0.05, min(float(grace_seconds), 10.0)))
            except subprocess.TimeoutExpired:
                self._kill_tree(handle, force=True)
        try:
            handle.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Still alive after the grace/force sequence: issue one last-resort
            # force kill so the slot is not held forever.
            self._kill_tree(handle, force=True)
        handle.refresh()
        with handle._lock:
            if handle.state != "running":
                handle.state = "killed" if force else "terminated"
                if handle.ended_at is None:
                    handle.ended_at = time.time()
            elif handle.proc.poll() is None:
                # The OS has not confirmed process exit. Do not release a live
                # process slot or report success: a failed kill must remain
                # observable so the caller can intervene or retry cleanup.
                raise ProcessError(f"process is still running after termination attempts: {process_id}")
        return handle

    def snapshot(self, process_id: str, *, max_chars: int = 12000) -> dict[str, Any]:
        handle = self.get(process_id)
        return self._snapshot_handle(handle, max_chars=max_chars)

    @staticmethod
    def _snapshot_handle(handle: ProcessHandle, *, max_chars: int = 12000) -> dict[str, Any]:
        handle.refresh()
        return {
            "process_id": handle.process_id,
            "pid": handle.pid,
            "state": handle.state,
            "exit_code": handle.exit_code,
            "command": handle.command,
            "cwd": str(handle.cwd),
            "created_at": handle.created_at,
            "ended_at": handle.ended_at,
            "stdout": handle.stdout.tail_text(max_chars),
            "stderr": handle.stderr.tail_text(max_chars),
        }

    def list(self, *, max_chars: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            self._prune_completed()
            handles = list(self._handles.values())
        return [self._snapshot_handle(handle, max_chars=max_chars) for handle in handles]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            ids = list(self._handles)
        for process_id in ids:
            try:
                self.terminate(process_id, force=False, grace_seconds=0.5)
            except ProcessError:
                pass
        with self._lock:
            handles = list(self._handles.values())
        for handle in handles:
            self._dispose_handle(handle)
