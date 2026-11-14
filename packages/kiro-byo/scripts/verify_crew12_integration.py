#!/usr/bin/env python3
"""Read-only KiroCrew stable 0.12.0+ source adapter preflight.

This is intentionally not a desktop acceptance test and performs no writes to
upstream Crew packages, backups or the BYO runtime state.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kiro_byo.integration_manager import _adapter_matches  # noqa: E402
from kiro_byo.crew_native_probe import inspect_native_registration  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crew-package", required=True, type=Path, help="installed .../kiro_crew Python package")
    # Optional: when a Crew package is absent the preflight refuses and reports
    # JSON regardless of version, so a caller probing "no upstream checkout"
    # must not be blocked by argparse before the refused result is emitted.
    parser.add_argument("--crew-version", default="", help="installed KiroCrew version, e.g. 0.12.0")
    args = parser.parse_args()
    profile = ROOT / "integration" / "kiro_crew" / "v0.12-plus"
    ok, issues, _manifest, compat = _adapter_matches(profile, args.crew_version, args.crew_package)
    print(json.dumps({
        "crew_version": args.crew_version,
        "profile": str(profile.relative_to(ROOT)),
        "source_contract_status": "pass" if ok else "refused",
        "windows_e2e": "not_verified",
        "verified_by_upstream_source_archive": False,
        "checks": len(compat.get("generated_files") or []),
        "issues": issues,
        "native_harness_registration": inspect_native_registration(args.crew_package),
    }, indent=2, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
