#!/usr/bin/env python3
"""Compare observable ACP tool behaviour between two KiroCrew backends.

This harness deliberately compares *runtime behaviour*, not advertised capability
flags. It launches two ACP commands, sends identical prompts, auto-approves
permission requests once, and records initial tool-call notifications plus the
final stop reason.

Example (PowerShell):
  py scripts/acp_parity_harness.py `
    --left-cmd "kiro-cli acp" `
    --right-cmd "python -m kiro_byo acp" `
    --cwd D:\\APP\\KiroCrewData\\workspace `
    --prompt "Read README.md" `
    --prompt "Create parity-test.txt containing hello, then read it back"
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class ToolEvent:
    title: str
    kind: str
    tool_call_id: str
    input_keys: list[str]


@dataclass
class PromptResult:
    prompt: str
    stop_reason: str
    tools: list[ToolEvent]
    elapsed_ms: float
    error: str = ""


class AcpProcess:
    def __init__(self, command: str, cwd: str) -> None:
        self.command = command
        self.cwd = cwd
        self.proc = subprocess.Popen(
            command,
            cwd=cwd or None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=True,
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self._next_id = 1
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._stderr_lines: list[str] = []
        self._stderr = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr.start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self.inbox.put(msg)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr_lines.append(line.rstrip())
            if len(self._stderr_lines) > 200:
                del self._stderr_lines[:50]

    def send(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any], timeout: float = 60.0) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        req_id = self._next_id
        self._next_id += 1
        self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.inbox.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"ACP process exited with {self.proc.returncode}: "
                        + "\n".join(self._stderr_lines[-20:])
                    )
                continue
            # Auto-approve one-shot host permission requests so both backends see
            # the same policy decision. The harness never grants 'always'.
            if msg.get("method") == "session/request_permission" and msg.get("id") is not None:
                self.send({
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
                })
                events.append(msg)
                continue
            events.append(msg)
            if msg.get("id") == req_id:
                return msg, events
        raise TimeoutError(f"ACP request timed out: {method}")

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)


def _tool_events(events: list[dict[str, Any]]) -> list[ToolEvent]:
    out: list[ToolEvent] = []
    seen: set[str] = set()
    for msg in events:
        if msg.get("method") != "session/update":
            continue
        update = (msg.get("params") or {}).get("update")
        if not isinstance(update, dict) or update.get("sessionUpdate") != "tool_call":
            continue
        call_id = str(update.get("toolCallId") or "")
        if call_id and call_id in seen:
            continue
        if call_id:
            seen.add(call_id)
        raw = update.get("rawInput")
        keys = sorted(str(k) for k in raw.keys()) if isinstance(raw, dict) else []
        out.append(ToolEvent(
            title=str(update.get("title") or ""),
            kind=str(update.get("kind") or ""),
            tool_call_id=call_id,
            input_keys=keys,
        ))
    return out


def run_backend(command: str, cwd: str, prompts: list[str], timeout: float) -> list[PromptResult]:
    backend = AcpProcess(command, cwd)
    try:
        init, _ = backend.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
                "clientInfo": {"name": "kiro-byo-parity-harness", "version": "1"},
            },
            timeout=min(timeout, 30),
        )
        if "error" in init:
            raise RuntimeError(f"initialize failed: {init['error']}")
        new, _ = backend.request(
            "session/new",
            {"cwd": cwd, "mcpServers": []},
            timeout=min(timeout, 30),
        )
        if "error" in new:
            raise RuntimeError(f"session/new failed: {new['error']}")
        session_id = str((new.get("result") or {}).get("sessionId") or "")
        if not session_id:
            raise RuntimeError("session/new returned no sessionId")

        results: list[PromptResult] = []
        for prompt in prompts:
            started = time.perf_counter()
            try:
                response, events = backend.request(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
                    timeout=timeout,
                )
                error = json.dumps(response.get("error"), ensure_ascii=False) if "error" in response else ""
                stop = str((response.get("result") or {}).get("stopReason") or "")
                results.append(PromptResult(
                    prompt=prompt,
                    stop_reason=stop,
                    tools=_tool_events(events),
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                    error=error,
                ))
            except Exception as exc:
                results.append(PromptResult(
                    prompt=prompt,
                    stop_reason="",
                    tools=[],
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                    error=f"{type(exc).__name__}: {exc}",
                ))
        return results
    finally:
        backend.close()


def signature(result: PromptResult) -> list[tuple[str, str]]:
    return [(event.title, event.kind) for event in result.tools]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--left-cmd", required=True, help="Native Kiro CLI ACP command")
    parser.add_argument("--right-cmd", required=True, help="Kiro BYO ACP command")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--prompt", action="append", required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    cwd = str(Path(args.cwd).resolve())
    left = run_backend(args.left_cmd, cwd, args.prompt, args.timeout)
    right = run_backend(args.right_cmd, cwd, args.prompt, args.timeout)
    rows: list[dict[str, Any]] = []
    all_equal = True
    for index, prompt in enumerate(args.prompt):
        l = left[index]
        r = right[index]
        equal = signature(l) == signature(r) and not l.error and not r.error
        all_equal = all_equal and equal
        rows.append({
            "prompt": prompt,
            "tool_sequence_equal": equal,
            "left": {**asdict(l), "tools": [asdict(t) for t in l.tools]},
            "right": {**asdict(r), "tools": [asdict(t) for t in r.tools]},
        })

    report = {
        "cwd": cwd,
        "left_cmd": args.left_cmd,
        "right_cmd": args.right_cmd,
        "all_tool_sequences_equal": all_equal,
        "cases": rows,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_out:
        Path(args.json_out).write_text(rendered + "\n", encoding="utf-8")
    return 0 if all_equal else 2


if __name__ == "__main__":
    raise SystemExit(main())
