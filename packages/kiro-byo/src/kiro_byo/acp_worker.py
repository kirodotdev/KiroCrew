"""ACP-agent-as-worker backend.

Kiro BYO normally executes an ``AgentTask`` through a raw model/provider. This
module offers a second backend that launches any stdio ACP agent and projects
its final answer into the same ``AgentResult`` contract.

External agents are treated as *workers*, never as trusted peers: permission
requests are denied by default and the parent decides which cwd and prompt the
external process receives.
"""
from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .agent_runtime import AgentResult, AgentTask
from .paths import app_home


class AcpWorkerError(RuntimeError):
    pass


@dataclass(frozen=True)
class AcpAgentDefinition:
    agent_id: str
    command: tuple[str, ...]
    title: str = ""
    cwd: str = ""
    timeout_seconds: float = 300.0
    enabled: bool = True

    def public_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "title": self.title or self.agent_id,
            "command": list(self.command),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "enabled": self.enabled,
        }


class AcpAgentCatalog:
    """Load user-configured external ACP workers from app-home config."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (app_home() / "config" / "acp_agents.json")

    def load(self) -> tuple[AcpAgentDefinition, ...]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        except (OSError, json.JSONDecodeError) as exc:
            raise AcpWorkerError(f"invalid ACP agent catalog: {exc}") from exc
        rows = raw.get("agents", raw) if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            raise AcpWorkerError("ACP agent catalog must contain an agents array")
        out: list[AcpAgentDefinition] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            agent_id = str(row.get("id") or row.get("agent_id") or "").strip()
            if not agent_id or agent_id in seen:
                continue
            command_raw = row.get("command")
            if isinstance(command_raw, str):
                command = tuple(shlex.split(command_raw, posix=os.name != "nt"))
            elif isinstance(command_raw, list):
                command = tuple(str(part) for part in command_raw if str(part))
            else:
                command = ()
            if not command:
                continue
            try:
                timeout = max(5.0, min(float(row.get("timeout_seconds", 300.0)), 3600.0))
            except (TypeError, ValueError):
                timeout = 300.0
            out.append(AcpAgentDefinition(
                agent_id=agent_id,
                command=command,
                title=str(row.get("title") or agent_id),
                cwd=str(row.get("cwd") or ""),
                timeout_seconds=timeout,
                enabled=bool(row.get("enabled", True)),
            ))
            seen.add(agent_id)
        return tuple(out)

    def get(self, agent_id: str) -> AcpAgentDefinition | None:
        return next((row for row in self.load() if row.agent_id == agent_id and row.enabled), None)


class AcpWorkerClient:
    def __init__(
        self,
        definition: AcpAgentDefinition,
        *,
        cwd: str,
        permission_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.definition = definition
        self.cwd = str(Path(definition.cwd or cwd or os.getcwd()).expanduser().resolve())
        self.permission_handler = permission_handler
        self._next_id = 1
        self._inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr: list[str] = []
        try:
            self.proc = subprocess.Popen(
                list(definition.command), cwd=self.cwd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            raise AcpWorkerError(f"failed to start ACP agent {definition.agent_id}: {exc}") from exc
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._err_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._err_reader.start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self._inbox.put(msg)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr.append(line.rstrip())
            if len(self._stderr) > 200:
                del self._stderr[:50]

    def _send(self, payload: dict[str, Any]) -> None:
        if self.proc.stdin is None:
            raise AcpWorkerError("ACP worker stdin is closed")
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    @staticmethod
    def _deny_permission() -> dict[str, Any]:
        return {"outcome": {"outcome": "cancelled"}}

    def request(
        self, method: str, params: dict[str, Any], *, timeout: float,
        cancel_event: threading.Event | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        req_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    if method == "session/prompt" and params.get("sessionId"):
                        self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": params["sessionId"]}})
                except (OSError, AcpWorkerError):
                    pass
                raise AcpWorkerError("ACP worker cancelled")
            try:
                msg = self._inbox.get(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                if self.proc.poll() is not None:
                    tail = "\n".join(self._stderr[-12:])
                    raise AcpWorkerError(f"ACP worker exited with {self.proc.returncode}: {tail}")
                continue
            if msg.get("method") == "session/request_permission" and msg.get("id") is not None:
                response = self.permission_handler(msg) if self.permission_handler is not None else self._deny_permission()
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": response})
                events.append(msg)
                continue
            events.append(msg)
            if msg.get("id") == req_id:
                return msg, events
        raise AcpWorkerError(f"ACP worker request timed out: {method}")

    def run_prompt(self, text: str, *, cancel_event: threading.Event | None = None) -> tuple[str, dict[str, Any]]:
        timeout = self.definition.timeout_seconds
        init, _ = self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
                "clientInfo": {"name": "kiro-byo-agent-worker", "version": "0.2.3"},
            },
            timeout=min(timeout, 30.0), cancel_event=cancel_event,
        )
        if "error" in init:
            raise AcpWorkerError(f"ACP initialize failed: {init['error']}")
        new, _ = self.request(
            "session/new", {"cwd": self.cwd, "mcpServers": []},
            timeout=min(timeout, 30.0), cancel_event=cancel_event,
        )
        if "error" in new:
            raise AcpWorkerError(f"ACP session/new failed: {new['error']}")
        session_id = str((new.get("result") or {}).get("sessionId") or "")
        if not session_id:
            raise AcpWorkerError("ACP session/new returned no sessionId")
        response, events = self.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
            timeout=timeout, cancel_event=cancel_event,
        )
        if "error" in response:
            raise AcpWorkerError(f"ACP prompt failed: {response['error']}")
        chunks: list[str] = []
        tools = 0
        for msg in events:
            if msg.get("method") != "session/update":
                continue
            update = (msg.get("params") or {}).get("update")
            if not isinstance(update, dict):
                continue
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content")
                if isinstance(content, dict) and content.get("type") == "text":
                    chunks.append(str(content.get("text") or ""))
            elif update.get("sessionUpdate") == "tool_call":
                tools += 1
        return "".join(chunks).strip(), {"tool_calls": tools, "stop_reason": str((response.get("result") or {}).get("stopReason") or "")}

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass


def run_acp_agent_task(task: AgentTask, definition: AcpAgentDefinition, *, cwd: str) -> AgentResult:
    """Execute an external ACP worker through the ordinary AgentResult seam."""
    started = time.perf_counter()
    objective = task.spec.objective
    context_lines: list[str] = []
    for row in task.context_messages[-24:]:
        if not isinstance(row, dict):
            continue
        content = row.get("content")
        if isinstance(content, str) and content.strip():
            context_lines.append(f"{str(row.get('role') or 'unknown')}: {content.strip()}")
    context = "\n".join(context_lines)
    if len(context) > 16000:
        context = context[-16000:]
    prompt = (
        "You are an isolated child agent delegated by another agent. "
        "Complete only the objective below and return concise findings to the parent. "
        "Do not assume you own the final user answer. Permission requests are fail-closed.\n\n"
        f"Objective:\n{objective}"
        + (f"\n\nParent context snapshot:\n{context}" if context else "")
    )
    client = AcpWorkerClient(definition, cwd=cwd)
    try:
        text, usage = client.run_prompt(prompt, cancel_event=task.cancel_event)
        return AgentResult(
            status="completed", summary=text, findings=(text,) if text else (),
            provider_id="acp", model_id=definition.agent_id,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            usage=usage,
        )
    except AcpWorkerError as exc:
        return AgentResult(
            status="cancelled" if task.cancel_event.is_set() else "failed",
            unresolved=(str(exc),), provider_id="acp", model_id=definition.agent_id,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
    finally:
        client.close()
