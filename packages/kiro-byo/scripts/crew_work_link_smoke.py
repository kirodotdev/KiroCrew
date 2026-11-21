#!/usr/bin/env python3
"""Isolated ACP protocol smoke for BYO's diagnostic-only Crew work-link seam.

Simulates an authorized host on a local ACP pipe; NOT a test of real Crew's
Work Ledger, authenticated dispatch, human Attention or gateway wiring.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts.acp_conformance import Process  # noqa: E402
from kiro_byo.crew_ledger_bridge import byo_work_correlation_advertised  # noqa: E402


def run() -> dict[str, Any]:
    checks: dict[str, bool] = {}
    proc = Process(f"{sys.executable} -m kiro_byo acp", str(ROOT))
    try:
        init = proc.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
            "clientInfo": {"name": "byo-test-host", "version": "1"},
        })
        checks["negotiated_diagnostic_only"] = byo_work_correlation_advertised(init.get("result"))
        new = proc.request("session/new", {"cwd": str(ROOT), "mcpServers": []})
        sid = str((new.get("result") or {}).get("sessionId") or "")
        if not sid:
            raise RuntimeError("ACP session/new rejected")
        link = {"taskId": "crew-work:0123456789abcdef0123456789abcdef", "role": "worker"}
        def bind(value: dict[str, str]) -> dict[str, Any]:
            return proc.request("_kiro/work/bind", {"sessionId": sid, "workLink": value})
        rejected = bind({"taskId": "../../not-trusted"})
        checks["invalid_link_rejected"] = (rejected.get("error") or {}).get("code") == -32602
        first = bind(link).get("result") or {}
        checks["bind_acknowledged"] = first.get("workLink") == link and first.get("changed") is True
        same = bind(link).get("result") or {}
        checks["idempotent_binding"] = same.get("changed") is False
        cleared = bind({}).get("result") or {}
        checks["explicit_detach"] = cleared.get("workLink") == {} and cleared.get("changed") is True
        proc.request("session/close", {"sessionId": sid})
        loaded = proc.request("session/load", {"sessionId": sid, "cwd": str(ROOT), "mcpServers": []})
        checks["load_after_detach"] = (loaded.get("result") or {}).get("sessionId") == sid
        checks["no_stale_task_after_load"] = (bind({}).get("result") or {}).get("changed") is False
        return {"ok": all(checks.values()), "checks": checks, "realCrewHost": "not_verified"}
    finally:
        proc.close()


def main() -> int:
    try:
        result = run()
    except (OSError, ValueError, RuntimeError, TimeoutError, KeyError, TypeError) as exc:
        result = {"ok": False, "checks": {}, "error": type(exc).__name__, "realCrewHost": "not_verified"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
