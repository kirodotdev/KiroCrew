#!/usr/bin/env python3
"""Exercise Mode -> Type -> Model selection over a real isolated BYO ACP child.

Uses fake .invalid providers without secrets and does not make model requests.
This protects BYO wire behaviour, NOT KiroCrew host integration or provider API.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

from acp_conformance import Process

ROOT = Path(__file__).resolve().parents[1]


def _check(ok: bool, label: str, checks: dict[str, bool]) -> None:
    checks[label] = bool(ok)
    if not ok:
        raise AssertionError(label)


def _state(reply: dict) -> tuple[str, list[str]]:
    if "error" in reply:
        raise AssertionError(f"ACP response error: {reply['error']}")
    result = reply.get("result") or {}
    models = result.get("models") or {}
    return models.get("currentModelId", ""), [row["modelId"] for row in models.get("availableModels", [])]


def _run_mode(pack: str, policy: str, kinds: tuple[str, ...]) -> dict:
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix=f"kiro-byo-{pack}-acp-") as directory:
        home = Path(directory) / "byo"
        (home / "config").mkdir(parents=True)
        (home / "config/providers.json").write_text(json.dumps({
            "credential_schema_version": 1,
            "mode": {"id": pack, "type": policy},
            "providers": [
                {"id": "fast", "name": "Fast", "type": "openai-compatible",
                 "base_url": "https://example.invalid/v1",
                 "models": [{"id": "fast", "name": "FastModel", "tier": "fast", "tool_use": True}]},
                {"id": "strong", "name": "Strong", "type": "openai-compatible",
                 "base_url": "https://example.invalid/v1",
                 "models": [{"id": "strong", "name": "StrongModel", "tier": "strong", "tool_use": True}]},
            ],
        }), encoding="utf-8")
        env_keys = ("KIRO_BYO_HOME", "KIROCREW_HOME", "PYTHONPATH")
        prior = {name: os.environ.get(name) for name in env_keys}
        try:
            os.environ.update({"KIRO_BYO_HOME": str(home), "KIROCREW_HOME": str(Path(directory) / "crew"),
                               "PYTHONPATH": str(ROOT / "src")})
            proc = Process(f"{sys.executable} -m kiro_byo acp", str(ROOT))
        finally:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        try:
            init = proc.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
            _check("error" not in init, "initialize", checks)
            p = {"cwd": str(ROOT), "mcpServers": []}
            new = proc.request("session/new", p)
            sid = new["result"]["sessionId"]
            current, ids = _state(new)
            expected = [f"kiro-byo-type:{pack}:{kind}" for kind in kinds]
            _check(current == f"kiro-byo-type:{pack}:{policy}", "default_type", checks)
            _check([i for i in ids if i.startswith("kiro-byo-type:")] == expected, "type_order", checks)
            _check({"FastModel", "StrongModel"}.issubset(ids), "concrete_models", checks)
            option = next(x for x in new["result"]["configOptions"] if x["id"] == "model")
            _check(option["currentValue"] == current and {x["value"] for x in option["options"]} == set(ids),
                   "v3_v1_catalogue_consistency", checks)
            pinned = proc.request("session/set_config_option", {"sessionId": sid, "configId": "model", "value": "StrongModel"})
            _check("error" not in pinned and any(x["currentValue"] == "StrongModel" for x in pinned["result"]["configOptions"]),
                   "pin_concrete_v3", checks)
            proc.request("session/close", {"sessionId": sid})
            current, _ = _state(proc.request("session/load", dict(p, sessionId=sid)))
            _check(current == "StrongModel", "pin_persists", checks)
            chosen = proc.request("session/set_model", {"sessionId": sid, "modelId": expected[-1]})
            _check("error" not in chosen, "select_type_legacy", checks)
            proc.request("session/close", {"sessionId": sid})
            current, _ = _state(proc.request("session/resume", dict(p, sessionId=sid)))
            _check(current == expected[-1], "type_persists", checks)
            foreign = "dyna" if pack == "tiga" else "tiga"
            denied = proc.request("session/set_config_option", {"sessionId": sid, "configId": "model",
                                                               "value": f"kiro-byo-type:{foreign}:power"})
            _check("error" in denied, "foreign_type_denied", checks)
            # A rejected model change must not mutate the ongoing selection.
            proc.request("session/close", {"sessionId": sid})
            current, _ = _state(proc.request("session/load", dict(p, sessionId=sid)))
            _check(current == expected[-1], "rejected_selection_atomic", checks)

            meta = {"kiro": {"modeId": "kirocrew-lite", "modelId": "auto",
                             "customAgents": [{"id": "kirocrew-lite", "prompt": "utility", "tools": []}]}}
            new_bg = proc.request("session/new", dict(p, _meta=meta))
            bid = new_bg["result"]["sessionId"]
            bg_current, bg_ids = _state(new_bg)
            _check(bg_current in {"FastModel", "StrongModel"} and
                   not any(i.startswith("kiro-byo-type:") for i in bg_ids), "background_is_concrete", checks)
            _check("error" in proc.request("session/set_model", {"sessionId": bid, "modelId": expected[0]}),
                   "background_rejects_hidden_type", checks)
            _check("error" in proc.request("session/set_config_option", {"sessionId": bid,
                    "configId": "model", "value": expected[-1]}),
                   "background_rejects_type_v3", checks)
            proc.request("session/close", {"sessionId": bid})
            bg_restored, _ = _state(proc.request("session/load", dict(p, sessionId=bid, _meta=meta)))
            _check(bg_restored == bg_current, "background_remains_concrete", checks)
            proc.request("session/delete", {"sessionId": sid})
            proc.request("session/delete", {"sessionId": bid})
        finally:
            proc.close()
    return {"mode": pack, "checks": checks}


def main() -> int:
    try:
        reports = [_run_mode("tiga", "multi", ("sky", "multi", "power", "glitter")),
                   _run_mode("dyna", "flash", ("miracle", "flash", "strong"))]
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "reports": reports}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
