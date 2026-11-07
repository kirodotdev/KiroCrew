#!/usr/bin/env python3
"""Read-only Crew runtime import/adaptor probe for an *isolated* 0.12 host.

This probe deliberately distinguishes registry membership from an importable ACP
runtime and from an actual `session/new`. It never changes a Crew package and it
never calls the model, tools or the provider. It is safe to run on an extracted
source tree but is not a production authorization attestation.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.probe_crew12_runtime_registration import probe as probe_registry  # noqa: E402

_CHILD = r'''
import json,sys,traceback
from pathlib import Path
sys.path.insert(0, sys.argv[1])
result={"runtimeImport":"not_verified", "adapterConstructed":False,
        "runtimeConstructed":False,"sessionNew":"not_run",
        "workerMcpAuthorization":"not_verified","desktopGateway":"not_verified"}
try:
    from kiro_crew.acp.harness import harness_for
    from kiro_crew.acp.runtime import AcpRuntime
    result["runtimeImport"]="pass"
    adapter=harness_for("kiro-byo")
    result["adapterConstructed"]=adapter.backend=="kiro-byo"
    import tempfile
    with tempfile.TemporaryDirectory(prefix="byo-crew-runtime-probe-") as work:
        runtime=AcpRuntime(work_dir=Path(work),acp_backend="kiro-byo", mcp_gateway_overlay=None)
        result["runtimeConstructed"]=runtime.acp_backend=="kiro-byo"
except ModuleNotFoundError as exc:
    result["runtimeImport"]="blocked_dependency"
    result["dependency"] = exc.name
except Exception as exc:
    result["runtimeImport"]="failed"
    result["errorType"] = type(exc).__name__
    result["error"] = str(exc)[:500]
print(json.dumps(result,ensure_ascii=False))
'''


def audit(crew_src: Path, *, timeout: float = 30.0) -> dict[str, Any]:
    crew_src = crew_src.resolve()
    if not (crew_src / "kiro_crew/agent_sdk/backends.py").is_file():
        raise ValueError("--crew-src must contain kiro_crew/agent_sdk/backends.py")
    registration = probe_registry(crew_src)
    env = dict(os.environ)
    # No site-packages mutation, no runtime launch, and no inherited Python module
    # shadowing. The child is a fresh interpreter for the real Crew source tree.
    env.pop("PYTHONPATH", None)
    try:
        child = subprocess.run(
            [sys.executable, "-I", "-c", _CHILD, str(crew_src)],
            text=True, capture_output=True, env=env, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        runtime = {"runtimeImport": "timed_out", "sessionNew": "not_run"}
    else:
        try:
            runtime = json.loads(child.stdout.strip())
        except json.JSONDecodeError:
            runtime = {
                "runtimeImport": "failed", "errorType": "InvalidChildResponse",
                "error": child.stderr[-700:], "sessionNew": "not_run",
            }
        if child.returncode and runtime.get("runtimeImport") == "pass":
            runtime["runtimeImport"] = "failed"
            runtime["errorType"] = "ChildExit"
            runtime["error"] = child.stderr[-700:]
    # The existing Crew tool_gate deliberately reports False for BYO. Never
    # use the successfully imported runtime to claim authorization was enforced.
    ready = (
        registration.get("selectable") is True
        and registration.get("hostLaunchDescriptionPresent") is True
        and runtime.get("runtimeImport") == "pass"
        and runtime.get("adapterConstructed") is True
        and runtime.get("runtimeConstructed") is True
    )
    return {
        "status": "runtime_constructor_pass" if ready else "blocked",
        "registry": registration,
        "runtime": runtime,
        "actualSessionNew": "not_run",
        "toolPermissionE2E": "not_verified",
        "nativeWithoutCorePatch": False,
        "desktopGatewayE2E": "not_verified",
        "meaning": "constructor probe only, not a session, permission, or desktop acceptance",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--crew-src", required=True, type=Path)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    report = audit(args.crew_src)
    data = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(data, encoding="utf-8")
    print(data, end="")
    return 0 if report["status"] == "runtime_constructor_pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
