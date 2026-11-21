#!/usr/bin/env python3
"""Read-only inventory of the BYO-to-KiroCrew source coupling.

A count of patched source files is *not* a host compatibility certificate. The
inventory is designed to make removal of invasive host patches measurable.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "integration/kiro_crew/v0.12-plus"


def owner_of(path: str) -> str:
    if path.startswith("src/kiro_crew/dashboard/"):
        return "dashboard"
    if path.startswith(("src/kiro_crew/members_parts/", "src/kiro_crew/config/")):
        return "crew_workflow"
    if path.startswith("src/kiro_crew/providers/"):
        return "provider_mirror"
    if path.startswith("src/kiro_crew/agent_sdk/") or path == "src/kiro_crew/acp_backends.py":
        return "backend_registry_and_policy"
    if path.startswith("src/kiro_crew/acp/"):
        return "acp_runtime"
    return "other"


def inventory(root: Path = ROOT) -> dict:
    profile = root / PROFILE.relative_to(ROOT)
    manifest = json.loads((profile / "manifest.json").read_text(encoding="utf-8"))
    compat = json.loads((profile / "compatibility.json").read_text(encoding="utf-8"))
    generated = compat.get("generated_files", [])
    created = compat.get("created_files", [])
    expected = manifest.get("files", [])
    symbols = compat.get("required_contract_symbols", {})
    markers = compat.get("required_contract_markers", {})
    exports = compat.get("required_contract_exports", {})
    errors = []
    if any(not isinstance(p, str) or not p.startswith("src/kiro_crew/") for p in generated + created):
        errors.append("host file inventory contains path outside Crew source")
    if len(generated + created) != len(set(generated + created)):
        errors.append("duplicate host file in mutation inventory")
    if set(generated + created) != set(expected):
        errors.append("manifest and compatibility mutation inventories differ")
    if (root / "src/kiro_byo/crew10_patch.py").exists() or (root / "src/kiro_byo/crew11_patch.py").exists():
        errors.append("retired adapter module remains in production package")
    dependency_files = set(symbols) | set(markers) | set(exports)
    return {
        "baseline": compat.get("min_version"),
        "mutatedHostFiles": len(generated),
        "createdHostFiles": len(created),
        "readOnlyContractDependencies": len(dependency_files - set(generated) - set(created)),
        "mutationGroups": {owner: sum(owner_of(p) == owner for p in generated)
                           for owner in sorted(set(map(owner_of, generated)))},
        "mutationPaths": list(generated),
        "createdPaths": list(created),
        "sourceTagValidated": False,  # Requires separately verified upstream bytes.
        "realHostE2EVerified": False,   # A manifest cannot establish this.
        "externalAppHarnessRegistrationVerified": False,
        "issues": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    args = parser.parse_args()
    result = inventory(args.root)
    output = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 1 if result["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
