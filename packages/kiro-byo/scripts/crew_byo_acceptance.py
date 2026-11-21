#!/usr/bin/env python3
"""Read-only BYO/Crew acceptance evidence matrix, never a live Host certification.

A BYO child-process ACP smoke proves only the child. KiroCrew source-contract
checks require an exact installed source package. No check here can substitute
for a real Crew Gateway/Mate/Cron/TaskRunner desktop acceptance run.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_crew12_integration import _adapter_matches  # noqa: E402
from scripts.verify_crew_work_ledger import inspect as inspect_work_ledger  # noqa: E402


def source_evidence(package: Path | None, version: str) -> dict[str, Any]:
    """Fail closed on absent/upstream-incompatible sources; never infer from tags."""
    if package is None or not package.is_dir():
        return {
            "crewSourceContract": "not_verified",
            "crewWorkLedger": "not_verified",
            "issues": ["exact installed KiroCrew source package not supplied"],
        }
    profile = ROOT / "integration" / "kiro_crew" / "v0.12-plus"
    ok, issues, _manifest, _compat = _adapter_matches(profile, version, package)
    ledger = inspect_work_ledger(package)
    return {
        "crewSourceContract": "pass" if ok else "refused",
        "crewWorkLedger": "pass" if ledger["work_ledger_source_surface_checked"] else "refused",
        "issues": [*issues, *ledger["issues"]],
    }


def smoke_child() -> dict[str, Any]:
    """Use an isolated home: never alter a real user's BYO settings or sessions."""
    with tempfile.TemporaryDirectory(prefix="kiro-byo-acp-acceptance-") as temp:
        env = dict(os.environ)
        env["KIRO_BYO_HOME"] = str(Path(temp) / "byo")
        env["KIROCREW_HOME"] = str(Path(temp) / "crew")
        env["PYTHONPATH"] = str(ROOT / "src")
        cmd = [sys.executable, str(ROOT / "scripts/acp_conformance.py"),
               "--cmd", f"{shlex.quote(sys.executable)} -m kiro_byo acp",
               "--cwd", str(ROOT), "--profile", "kirocrew"]
        try:
            result = subprocess.run(cmd, env=env, cwd=ROOT, text=True,
                                    capture_output=True, timeout=65, check=False)
        except subprocess.TimeoutExpired:
            return {"status": "failed", "issues": ["isolated ACP smoke timed out"]}
        if result.returncode:
            return {"status": "failed", "issues": ["BYO ACP child smoke failed"],
                    "exitCode": result.returncode}
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"status": "failed", "issues": ["ACP smoke did not emit JSON"]}
        return {"status": "pass" if report.get("ok") else "failed",
                "checks": report.get("checks", {}), "isolatedHome": True}


def smoke_work_link() -> dict[str, Any]:
    """Check ACP diagnostic-link lifecycle in a separate, isolated test home."""
    with tempfile.TemporaryDirectory(prefix="kiro-byo-work-link-") as temp:
        env = dict(os.environ)
        env["KIRO_BYO_HOME"] = str(Path(temp) / "byo")
        env["KIROCREW_HOME"] = str(Path(temp) / "crew")
        env["PYTHONPATH"] = str(ROOT / "src")
        try:
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/crew_work_link_smoke.py")],
                cwd=ROOT, env=env, text=True, capture_output=True,
                timeout=45, check=False,
            )
        except subprocess.TimeoutExpired:
            return {"status": "failed", "issues": ["work-link ACP smoke timed out"]}
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"status": "failed", "issues": ["work-link smoke did not emit JSON"]}
        return {"status": "pass" if result.returncode == 0 and parsed.get("ok") else "failed",
                "checks": parsed.get("checks", {}), "isolatedHome": True}


def smoke_mode_type() -> dict[str, Any]:
    """Verify interactive choices over ACP, independently of Crew patches."""
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/acp_mode_type_smoke.py")],
            cwd=ROOT, text=True, capture_output=True, timeout=90, check=False,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
    except subprocess.TimeoutExpired:
        return {"status": "failed", "issues": ["Mode/Type/Model ACP smoke timed out"]}
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "failed", "issues": ["Mode/Type/Model smoke did not emit JSON"]}
    return {
        "status": "pass" if result.returncode == 0 and report.get("ok") else "failed",
        "modes": report.get("reports", []),
        "issues": [report["error"]] if report.get("error") else [],
        "isolatedHome": True,
        "liveProviderRequests": False,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--crew-package", type=Path)
    p.add_argument("--crew-version", default="")
    p.add_argument("--smoke", action="store_true", help="launch isolated BYO ACP child")
    p.add_argument("--require-source", action="store_true", help="fail without compatible Crew source")
    args = p.parse_args()
    source = source_evidence(args.crew_package, args.crew_version)
    child = smoke_child() if args.smoke else {"status": "not_run"}
    work_link = smoke_work_link() if args.smoke else {"status": "not_run"}
    mode_type = smoke_mode_type() if args.smoke else {"status": "not_run"}
    report = {
        "crewVersionClaim": args.crew_version or "not_supplied",
        "byoAcpChild": child,
        "byoWorkLinkProtocol": work_link,
        "byoModeTypeModelProtocol": mode_type,
        **source,
        "crewGatewayE2E": "not_verified",
        "mateWorkerE2E": "not_verified",
        "cronE2E": "not_verified",
        "taskRunnerE2E": "not_verified",
        "humanAttentionFromCrew": "not_verified",
        "certified": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if any(row["status"] == "failed" for row in (child, work_link, mode_type)):
        return 2
    if args.require_source and (source["crewSourceContract"] != "pass" or source["crewWorkLedger"] != "pass"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
