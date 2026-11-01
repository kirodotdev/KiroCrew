#!/usr/bin/env python3
"""Read-only semantic preflight against the installed Crew 0.11 RC source.

An adapter is installable only when every upstream seam and import anchor is
recognized and the proposed generated source compiles. This does not prove
runtime E2E behavior; a missing/moved Mate 2.0 owner must still be audited.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from kiro_byo.integration_manager import (
    _adapter_matches,
    _converge_semantic_transform,
    _normalized_text,
    _transformer_for,
    _crew11_semantic_candidate,
)

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "integration" / "kiro_crew" / "v0.11-rc1"
PREFIX = "src/kiro_crew/"


def preflight(crew_package: Path, version: str) -> dict:
    issues = []
    if not crew_package.is_dir():
        issues.append("Crew src/kiro_crew directory not found")
    if version not in {"0.11.0-rc.1", "0.11.0rc1"} and not _crew11_semantic_candidate(version):
        issues.append("Only audited 0.11.0-rc.1, guarded 0.11.0-rc.2+ / 0.11.1-rc.1+, or exact 0.11.0 / 0.11.1 source-contract candidates accepted")
    manifest = json.loads((ADAPTER / "manifest.json").read_text(encoding="utf-8"))
    compat = json.loads((ADAPTER / "compatibility.json").read_text(encoding="utf-8"))
    if not issues:
        ok, details, _, _ = _adapter_matches(ADAPTER, version, crew_package)
        if not ok:
            issues.extend(details)
        else:
            targets, patch_error, inspect, patch = _transformer_for(compat)
            for relative in compat.get("generated_files", []):
                try:
                    assert relative in targets, f"No patch transformer for {relative}"
                    path = crew_package / relative[len(PREFIX):]
                    proposed = _converge_semantic_transform(
                        relative, _normalized_text(path), inspect_fn=inspect,
                        transform_fn=patch, transform_error=patch_error,
                    )
                    compile(proposed, relative, "exec")
                except Exception as exc:
                    issues.append(f"{relative}: {type(exc).__name__}: {exc}")
    is_audited = version in {"0.11.0-rc.1", "0.11.0rc1"}
    return {
        "target": version,
        "adapter": "v0.11-rc1",
        "eligible_to_attempt_install": not issues,
        "source_audited_by_maintainer": False,
        "exact_rc1_source_pins_match": bool(
            not issues and is_audited and all(
                (crew_package / rel[len(PREFIX):]).is_file() and
                __import__("hashlib").sha256(
                    (crew_package / rel[len(PREFIX):]).read_text(encoding="utf-8-sig")
                    .replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
                ).hexdigest() in pins.values()
                for rel, pins in compat.get("pinned_source_sha256", {}).items()
            )
        ),
        "provisional_source_checked": bool(not issues and not is_audited),
        "pinned_seams_checked": len(compat.get("pinned_source_sha256", {})),
        "mate_worker_kit_supported": bool(not issues),
        "desktop_e2e_verified": False,
        "mate2_conductor_worker_contract_verified": bool(not issues),
        "issues": issues,
        "seams_checked": len(manifest.get("files", [])),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--crew-package", required=True, type=Path)
    p.add_argument("--crew-version", default="0.11.0-rc.1")
    p.add_argument("--output", type=Path, help="optional JSON report path")
    args = p.parse_args()
    report = preflight(args.crew_package.resolve(), args.crew_version)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0 if report["eligible_to_attempt_install"] else 2

if __name__ == "__main__":
    sys.exit(main())
