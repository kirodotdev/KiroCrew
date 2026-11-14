#!/usr/bin/env python3
"""Audit exact Crew 0.12 source files without installing or requiring UI assets.

The official BYO-source subset contains no compiled dashboard. This probe never
calls it installable and never writes to Crew. Use verify_crew12_integration.py
against a FULL installed Crew package for the strict deployable preflight.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kiro_byo import crew12_patch  # noqa: E402
from kiro_byo.crew_contract_checks import (  # noqa: E402
    compatibility_contract_issues, rc6_contract_issues,
)
from kiro_byo.crew_native_probe import inspect_native_registration  # noqa: E402
from kiro_byo.crew_contract_checks import retired_tool_gate_contract_issues  # noqa: E402


def permission_evidence(crew_package: Path) -> dict:
    """Separate source-advertised BYO routing from enforceable Host evidence.

    This is intentionally a negative/indeterminate probe: static declarations
    cannot prove actual deny/allow or Worker/MCP coverage.
    """
    path = crew_package / "agent_sdk" / "tool_gate.py"
    if not path.is_file() or path.is_symlink():
        return {"status": "source_unavailable", "runtimeDenyAllowVerified": False}
    original = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(original)
    assignments = (
        node for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(isinstance(t, ast.Name) and t.id == "ENFORCED_ROUTINGS"
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))
    )
    enforced = list(assignments)
    explicitly_enforced = len(enforced) == 1 and any(
        isinstance(item, ast.Attribute)
        and isinstance(item.value, ast.Name)
        and item.value.id == "Routing"
        and item.attr == "ACP_PERMISSION_GATE"
        for item in ast.walk(enforced[0].value)
    )
    verdict = next((fn for fn in tree.body
                    if isinstance(fn, ast.FunctionDef) and fn.name == "routing_verdict"), None)
    advertised_routed = bool(verdict and any(
        isinstance(n, ast.Attribute) and n.attr == "ACP_PERMISSION_GATE"
        for n in ast.walk(verdict)))
    return {
        "status": "runtime_permission_evidence_missing",
        "byoPermissionBranchPresent": advertised_routed,
        "hostUnknownRoutingFallbackAudited": not retired_tool_gate_contract_issues(crew_package),
        "byoInHostEnforcedRoutings": explicitly_enforced,
        "runtimeDenyAllowVerified": False,
        "workerMcpPermissionVerified": False,
        "note": "The original Crew unknown-routing fallback can be audited without modification, but cannot certify actual native/MCP/Worker enforcement; require live deny/allow evidence.",
    }


def audit(crew_package: Path) -> dict:
    profile = ROOT / "integration/kiro_crew/v0.12-plus"
    compat = json.loads((profile / "compatibility.json").read_text(encoding="utf-8"))
    generated = compat["generated_files"]
    patches = []
    issues = []
    if set(generated) != set(crew12_patch.TARGET_FILES):
        issues.append("the integration manifest and 0.12 patcher do not cover the same active mutation owners")
    for relative in generated:
        destination = crew_package / relative.removeprefix("src/kiro_crew/")
        row = {"path": relative, "state": "missing"}
        try:
            if destination.is_symlink() or not destination.is_file():
                raise ValueError("source not a regular file")
            source = destination.read_text(encoding="utf-8-sig")
            row["sha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
            inspected = crew12_patch.inspect(relative, source)
            row["state"] = inspected.state
            row["detail"] = inspected.detail
            if inspected.state != "clean":
                raise ValueError("expected pristine Crew source, got " + inspected.state)
            candidate = crew12_patch.patch_text(relative, source)
            compile(candidate, relative, "exec")
            if candidate != crew12_patch.patch_text(relative, candidate):
                raise ValueError("non-idempotent patch")
            if crew12_patch.inspect(relative, candidate).state != "patched":
                raise ValueError("candidate did not converge")
            row["state"] = "source_transform_pass"
        except (OSError, UnicodeError, ValueError, SyntaxError, crew12_patch.PatchError) as exc:
            row["error"] = str(exc)
            issues.append(f"{relative}: {exc}")
        patches.append(row)
    issues.extend(compatibility_contract_issues(compat, crew_package))
    issues.extend(rc6_contract_issues(crew_package))
    dashboard = crew_package / "static/dist/index.html"
    ui_present = dashboard.is_file() and not dashboard.is_symlink()
    native = inspect_native_registration(crew_package)
    return {
        "input": "KiroCrew source package (no source changes)",
        "sourceTransformStatus": "pass" if not issues else "refused",
        "sourceOnlyNotInstallable": not ui_present,
        "dashboardStaticAssets": "present_not_yet_verified" if ui_present else "not_in_source_subset",
        "nativeExternalBackendSupported": native.get("status") == "core_registration_candidate",
        "registration": native,
        "sourceTransformFilesPassed": sum(p["state"] == "source_transform_pass" for p in patches),
        "sourceTransformFilesExpected": len(generated),
        "hostIntegrationE2E": "not_verified",
        "permissionsE2E": "not_verified",
        "permissionEvidence": permission_evidence(crew_package),
        "issues": issues,
        "files": patches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crew-package", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.crew_package)
    result = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    print(result, end="")
    return 0 if report["sourceTransformStatus"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
