"""Black-box ACP editor client for the conformance gate.

Spawns the real ``kirocrew acp`` entrypoint as an external subprocess and speaks
newline-delimited JSON-RPC 2.0 over its stdin/stdout, exactly as an ACP editor
(Zed, VS Code) would. It imports **no** server internals — the only coupling to
the implementation is the pinned wire surface in :mod:`acp_bb_schema`, through
which every received frame is validated automatically.

Design notes:

* A single reader thread parses each stdout line, validates it, and routes it:
  responses wake :meth:`wait_response`; ``session/update`` notifications and
  agent->client ``session/request_permission`` requests are recorded.
* Permission requests are answered from the reader thread (so a prompt that
  blocks on one can still complete) unless ``permission_mode="manual"``, in which
  case the test answers them itself — used by the cancel-mid-permission test.
* Nothing blocks the reader thread on a test-controlled event, so the pipe is
  always drained and the suite cannot deadlock; every wait is bounded.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import acp_bb_schema as schema

# Path to the in-repo ``src`` so the spawned adapter runs feature-branch code.
_SRC = str(Path(__file__).resolve().parent.parent / "src")

# A real, stdlib-only stdio MCP server: initialize + tools/list (echo + a
# permission-gated write_file) + tools/call. Launched via ``sys.executable -c``,
# matching the fixture convention already used by the Phase-2 MCP tests. Used to
# prove the adapter's session/new MCP preflight performs a genuine spawn +
# ``initialize`` handshake through the sandbox chokepoint.
ECHO_MCP_SCRIPT = r"""
import sys, json
def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        m = json.loads(line)
    except Exception:
        continue
    mid, method = m.get("id"), m.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "serverInfo": {"name": "fixture-echo", "version": "0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": "echo", "description": "echo text",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
            {"name": "write_file", "description": "permission-gated write",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}}]}})
    elif method == "tools/call":
        args = (m.get("params") or {}).get("arguments") or {}
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": "echo:" + str(args.get("text", ""))}]}})
    elif method and method.startswith("notifications/"):
        pass
"""


def echo_mcp_server(name: str = "echo") -> dict[str, Any]:
    """A canonical ACP stdio ``mcpServers`` entry running the fixture server."""
    return {"name": name, "command": sys.executable, "args": ["-c", ECHO_MCP_SCRIPT]}


class AcpEditor:
    """A black-box ACP client driving one ``kirocrew acp`` subprocess."""

    def __init__(
        self,
        gateway_url: str,
        *,
        home: str,
        agent: str | None = "kirocrew",
        permission_mode: str = "deny",
        ready_timeout: float = 30.0,
    ) -> None:
        self._gateway_url = gateway_url
        self._home = home
        self._agent = agent
        self.permission_mode = permission_mode  # "deny" | "allow" | "manual"
        self._ready_timeout = ready_timeout

        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_buf: list[bytes] = []

        self._next_id = 1
        self._id_method: dict[Any, str] = {}
        self._write_lock = threading.Lock()
        self._cond = threading.Condition()
        self._responses: dict[Any, dict[str, Any]] = {}

        self.all_frames: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.permission_requests: list[dict[str, Any]] = []
        self.schema_errors: list[str] = []

    # ── lifecycle ──
    def __enter__(self) -> "AcpEditor":
        cmd = [sys.executable, "-m", "kiro_crew", "acp", "--gateway-url", self._gateway_url]
        if self._agent:
            cmd += ["--agent", self._agent]
        cmd.append("--verbose")
        env = {
            **os.environ,
            "PYTHONPATH": _SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "KIROCREW_HOME": self._home,
            "PYTHONUNBUFFERED": "1",
            "KIROCREW_SKIP_MODEL_DOWNLOAD": "1",
        }
        self._proc = subprocess.Popen(
            cmd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self, timeout: float = 10.0) -> int | None:
        """Close stdin (adapter EOF), wait for a clean exit, killpg on timeout."""
        proc = self._proc
        if proc is None:
            return None
        with self._write_lock:
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._killpg()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
        return proc.returncode

    def _killpg(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    def stderr_tail(self, n: int = 3000) -> str:
        return b"".join(self._stderr_buf).decode("utf-8", "replace")[-n:]

    # ── io threads ──
    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        stderr = proc.stderr
        for chunk in iter(lambda: stderr.read1(4096), b""):  # type: ignore[attr-defined]
            self._stderr_buf.append(chunk)

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except (ValueError, TypeError):
                self.schema_errors.append(f"agent wrote non-JSON to stdout: {line!r}")
                continue
            self.all_frames.append(frame)
            try:
                role = schema.validate_frame(frame, self._id_method.get)
            except schema.AcpSchemaError as exc:
                self.schema_errors.append(str(exc))
                role = _role_hint(frame)
            self._route(frame, role)

    def _route(self, frame: dict[str, Any], role: str) -> None:
        if role in ("result", "error"):
            with self._cond:
                self._responses[frame.get("id")] = frame
                self._cond.notify_all()
            return
        method = frame.get("method")
        if method == "session/update":
            self.updates.append(frame)
        elif method == "session/request_permission":
            self.permission_requests.append(frame)
            if self.permission_mode in ("allow", "deny"):
                self.answer_permission(frame["id"], allow=self.permission_mode == "allow")

    # ── sending ──
    def _write(self, obj: dict[str, Any]) -> None:
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        data = (json.dumps(obj) + "\n").encode("utf-8")
        with self._write_lock:
            proc.stdin.write(data)
            proc.stdin.flush()

    def send_raw(self, data: bytes) -> None:
        """Write raw bytes to stdin (for malformed-frame conformance cases)."""
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        with self._write_lock:
            proc.stdin.write(data)
            proc.stdin.flush()

    def send_request_async(self, method: str, params: dict[str, Any]) -> Any:
        req_id = self._next_id
        self._next_id += 1
        self._id_method[req_id] = method
        self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return req_id

    def wait_response(self, req_id: Any, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while req_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"no response for id {req_id} within {timeout}s; "
                        f"stderr tail:\n{self.stderr_tail()}"
                    )
                self._cond.wait(timeout=min(remaining, 0.25))
            return self._responses[req_id]

    def request(self, method: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        return self.wait_response(self.send_request_async(method, params), timeout)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def answer_permission(self, agent_request_id: Any, *, allow: bool) -> None:
        option = "allow_once" if allow else "reject_once"
        self._write(
            {
                "jsonrpc": "2.0",
                "id": agent_request_id,
                "result": {"outcome": {"outcome": "selected", "optionId": option}},
            }
        )

    # ── ACP method helpers ──
    def initialize(self, protocol_version: int = 1, timeout: float = 30.0) -> dict[str, Any]:
        return self.request("initialize", {"protocolVersion": protocol_version}, timeout)

    def session_new(
        self,
        cwd: str = "/tmp",
        mcp_servers: list[dict[str, Any]] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": cwd}
        if mcp_servers is not None:
            params["mcpServers"] = mcp_servers
        return self.request("session/new", params, timeout)

    def session_load(
        self, session_id: str, cwd: str = "/tmp", timeout: float = 30.0
    ) -> dict[str, Any]:
        return self.request("session/load", {"sessionId": session_id, "cwd": cwd}, timeout)

    def session_resume(
        self, session_id: str, cwd: str = "/tmp", timeout: float = 30.0
    ) -> dict[str, Any]:
        return self.request("session/resume", {"sessionId": session_id, "cwd": cwd}, timeout)

    def session_set_mode(
        self, session_id: str, mode_id: str, timeout: float = 30.0
    ) -> dict[str, Any]:
        return self.request(
            "session/set_mode", {"sessionId": session_id, "modeId": mode_id}, timeout
        )

    def session_set_config_option(
        self,
        session_id: str,
        config_id: str,
        value: Any,
        *,
        boolean: bool = False,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"sessionId": session_id, "configId": config_id, "value": value}
        if boolean:
            params["type"] = "boolean"
        return self.request("session/set_config_option", params, timeout)

    def session_list(
        self, cwd: str | None = None, cursor: str | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cwd is not None:
            params["cwd"] = cwd
        if cursor is not None:
            params["cursor"] = cursor
        return self.request("session/list", params, timeout)

    def prompt(self, session_id: str, text_or_blocks: Any, timeout: float = 30.0) -> dict[str, Any]:
        return self.wait_response(self.prompt_async(session_id, text_or_blocks), timeout)

    def prompt_async(self, session_id: str, text_or_blocks: Any) -> Any:
        if isinstance(text_or_blocks, str):
            blocks: list[dict[str, Any]] = [{"type": "text", "text": text_or_blocks}]
        else:
            blocks = list(text_or_blocks)
        return self.send_request_async(
            "session/prompt", {"sessionId": session_id, "prompt": blocks}
        )

    def cancel(self, session_id: str) -> None:
        self.notify("session/cancel", {"sessionId": session_id})

    # ── assertions/waits ──
    def assert_conformant(self) -> None:
        if self.schema_errors:
            raise AssertionError(
                "ACP schema violations in emitted frames:\n- " + "\n- ".join(self.schema_errors)
            )

    def wait_update(
        self, pred: Callable[[dict[str, Any]], bool], timeout: float = 15.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in list(self.updates):
                if pred(frame):
                    return frame
            time.sleep(0.02)
        raise AssertionError(
            f"no session/update matched within {timeout}s; updates={self.updates!r}"
        )

    def wait_permission(self, timeout: float = 15.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.permission_requests:
                return self.permission_requests[0]
            time.sleep(0.02)
        raise AssertionError(
            f"no session/request_permission within {timeout}s;\n{self.stderr_tail()}"
        )


def _role_hint(frame: dict[str, Any]) -> str:
    if "result" in frame or "error" in frame:
        return "result" if "result" in frame else "error"
    if frame.get("method") is not None and "id" in frame:
        return "request"
    return "notification"


def agent_message_text(update_frame: dict[str, Any]) -> str:
    """Extract the text of an agent_message_chunk session/update frame."""
    return update_frame["params"]["update"]["content"]["text"]
