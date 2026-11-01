"""Cross-platform end-to-end ACP wire smoke without a provider or live Crew.

Runs the real CLI subprocess and validates JSON-RPC responses over stdio.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_acp_stdio_wire_initialize_and_reject_invalid_operations(tmp_path: Path) -> None:
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": 1, "clientInfo": {"name": "stage2-wire-smoke", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/set_model",
         "params": {"sessionId": "missing", "modelId": "not-a-model"}},
        {"jsonrpc": "2.0", "id": 3, "method": "session/set_config_option",
         "params": {"sessionId": "missing", "configId": "model", "value": "not-a-model"}},
        {"jsonrpc": "2.0", "id": 4, "method": "unexpected/method", "params": {}},
    ]
    env = dict(os.environ)
    env["KIRO_BYO_HOME"] = str(tmp_path)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [source_root, env.get("PYTHONPATH", "")]))
    proc = subprocess.run(
        [sys.executable, "-m", "kiro_byo", "acp"],
        input="".join(json.dumps(x) + "\n" for x in frames),
        text=True, capture_output=True, env=env, timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    messages = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    by_id = {m["id"]: m for m in messages if "id" in m}
    assert set(by_id) == {1, 2, 3, 4}
    assert by_id[1]["result"]["protocolVersion"] == 1
    assert by_id[2]["error"]["code"] == -32602
    assert by_id[3]["error"]["code"] == -32602
    assert by_id[4]["error"]["code"] == -32601
    assert all(m.get("jsonrpc") == "2.0" for m in messages)
