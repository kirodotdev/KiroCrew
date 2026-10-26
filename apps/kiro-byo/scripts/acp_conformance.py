#!/usr/bin/env python3
"""Run a small stable-ACP-v1 conformance smoke against an ACP command.

This is intentionally client-neutral. ``--profile`` labels the report with a
known client family (generic/KiroCrew/Zed/JetBrains/Qwen Code) but does not
pretend to emulate proprietary client internals.
"""
from __future__ import annotations

import argparse
import json
import queue
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from kiro_byo.acp_conformance import PROFILES, validate_initialize


class Process:
    def __init__(self, command: str, cwd: str) -> None:
        self.proc = subprocess.Popen(
            shlex.split(command), cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self.next_id = 1
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self.inbox.put(msg)

    def request(self, method: str, params: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
        rid = self.next_id
        self.next_id += 1
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc":"2.0","id":rid,"method":method,"params":params}) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.inbox.get(timeout=0.2)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"ACP process exited with {self.proc.returncode}")
                continue
            if msg.get("id") == rid:
                return msg
            if msg.get("method") == "session/request_permission" and msg.get("id") is not None:
                self.proc.stdin.write(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"outcome":{"outcome":"cancelled"}}}) + "\n")
                self.proc.stdin.flush()
        raise TimeoutError(method)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc":"2.0","method":method,"params":params}) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cmd", required=True)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--profile", default="generic", choices=[row.profile_id for row in PROFILES])
    args = parser.parse_args()
    cwd = str(Path(args.cwd).resolve())
    proc = Process(args.cmd, cwd)
    report: dict[str, Any] = {"profile": args.profile, "cwd": cwd, "checks": {}}
    try:
        init = proc.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs":{"readTextFile":True,"writeTextFile":True}},
            "clientInfo": {"name":f"kiro-byo-conformance-{args.profile}","version":"1"},
        })
        init_result = init.get("result") if isinstance(init.get("result"), dict) else {}
        issues = validate_initialize(init_result)
        report["checks"]["initialize"] = {"ok": "error" not in init and not issues, "issues": list(issues)}
        new = proc.request("session/new", {"cwd": cwd, "mcpServers": []})
        sid = str((new.get("result") or {}).get("sessionId") or "")
        report["checks"]["session_new"] = {"ok": bool(sid) and "error" not in new}
        if sid:
            mode_id = str((((new.get("result") or {}).get("modes") or {}).get("currentModeId")) or "")
            prompt = proc.request("session/prompt", {
                "sessionId": sid,
                "prompt": [{"type":"text","text":"ACP conformance echo"}],
            })
            report["checks"]["session_prompt"] = {"ok": "error" not in prompt}
            if mode_id:
                set_mode = proc.request("session/set_mode", {"sessionId": sid, "modeId": mode_id})
                report["checks"]["session_set_mode"] = {"ok": "error" not in set_mode}
            proc.notify("session/cancel", {"sessionId": sid})
            time.sleep(0.05)
            report["checks"]["session_cancel_notification"] = {"ok": proc.proc.poll() is None}
            listed = proc.request("session/list", {})
            sessions = (listed.get("result") or {}).get("sessions") if isinstance(listed.get("result"), dict) else None
            report["checks"]["session_list"] = {"ok": isinstance(sessions, list)}
            closed = proc.request("session/close", {"sessionId": sid})
            report["checks"]["session_close"] = {"ok": "error" not in closed}
            loaded = proc.request("session/load", {"sessionId": sid, "cwd": cwd, "mcpServers": []})
            report["checks"]["session_load"] = {"ok": "error" not in loaded and str((loaded.get("result") or {}).get("sessionId") or "") == sid}
            closed_again = proc.request("session/close", {"sessionId": sid})
            report["checks"]["session_close_after_load"] = {"ok": "error" not in closed_again}
            resumed = proc.request("session/resume", {"sessionId": sid, "cwd": cwd, "mcpServers": []})
            report["checks"]["session_resume"] = {"ok": "error" not in resumed and str((resumed.get("result") or {}).get("sessionId") or "") == sid}
            deleted = proc.request("session/delete", {"sessionId": sid})
            report["checks"]["session_delete"] = {"ok": "error" not in deleted}
        report["ok"] = all(row.get("ok") for row in report["checks"].values())
    finally:
        proc.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
