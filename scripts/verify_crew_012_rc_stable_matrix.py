#!/usr/bin/env python3
"""Reproduce guarded RC/stable 0.12 installation on the SAME extracted Crew tree.

Never touches a real installed KiroCrew or user configuration.  The supplied
source and Dashboard archives must be checked against the actual installed RC
build separately; this is a source-contract compatibility experiment only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from verify_crew12_dashboard_archives import (
    SOURCE_PREFIX, DASHBOARD_PREFIX, extract_owned_tree, missing_local_resources,
)

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kiro_byo import integration_manager as manager  # noqa: E402
from probe_crew12_runtime_registration import probe  # noqa: E402


def _hashes(crew: Path, paths: list[str]) -> dict[str, str]:
    return {p: hashlib.sha256((crew / p).read_bytes()).hexdigest() for p in paths}


def check(source: Path, dashboard: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="byo-crew-012-matrix-") as temp:
        root = Path(temp)
        crew = root / "crew" / "src" / "kiro_crew"
        source_info = extract_owned_tree(source, SOURCE_PREFIX, crew)
        dashboard_info = extract_owned_tree(dashboard, DASHBOARD_PREFIX, crew / "static/dist")
        profile = ROOT / "integration/kiro_crew/v0.12-plus"
        compat = json.loads((profile / "compatibility.json").read_text(encoding="utf8"))
        original_paths = [p.removeprefix("src/kiro_crew/") for p in compat["generated_files"]]
        original_paths.append("static/dist/index.html")
        created = [p.removeprefix("src/kiro_crew/") for p in compat["created_files"]]
        before = _hashes(crew, original_paths)
        original_registry = probe(crew.parent)
        runs = []
        for version in ("0.12.0-rc.10", "0.12.0"):
            ok, issues, _, _ = manager._adapter_matches(profile, version, crew)
            if not ok:
                raise RuntimeError(f"{version}: strict source check refused: {issues[:5]}")
            home = root / f"byo-home-{version}"
            with patch.object(manager, "running_crew", lambda v=version: (v, crew, crew.parent)):
                install = manager.install_from_app(ROOT, home, version=(ROOT / "VERSION").read_text(encoding="utf8").strip())
                if not install.get("ok"):
                    raise RuntimeError(f"{version}: install returned {install}")
                integrated_registry = probe(crew.parent)
                if (not integrated_registry.get("selectable")
                        or not integrated_registry.get("hostLaunchDescriptionPresent")):
                    raise RuntimeError(f"{version}: BYO did not register after integration")
                if integrated_registry.get("hostNonRoutedRefusalEnforced"):
                    raise RuntimeError(f"{version}: unsafe proof claim; host should remain unverified")
                repeat = manager.install_from_app(ROOT, home, version=(ROOT / "VERSION").read_text(encoding="utf8").strip())
                if not repeat.get("ok"):
                    raise RuntimeError(f"{version}: repeat install failed")
                uninstall = manager.uninstall_from_app(ROOT, home)
                if not uninstall.get("ok"):
                    raise RuntimeError(f"{version}: uninstall failed")
            restored = _hashes(crew, original_paths) == before
            cleaned = all(not (crew / p).exists() for p in created)
            if not (restored and cleaned):
                raise RuntimeError(f"{version}: uninstall left host changes behind")
            runs.append({
                "versionLabel": version, "strictSourcePreflight": "pass",
                "install": "pass", "reinstall": "pass", "uninstall": "pass",
                "restoredHostFileCount": len(original_paths),
                "hostRegistrySelectableAfterPatch": True,
                "hostPermissionEnforced": False,
                "hostOriginalFilesRestored": restored,
            })
        return {
            "result": "pass", "crewSource": source_info,
            "dashboard": dashboard_info,
            "sourceAndDashboardArchivesMatchInstalledRC": "not_verified",
            "originalCrewRegisteredBYO": original_registry["registration"]["allowed"],
            "staticResourceGaps": missing_local_resources(crew / "static/dist"),
            "runs": runs, "realWindowsGatewayE2E": "not_verified",
            "nativePatchlessIntegration": "not_supported_by_supplied_Crew_0.12",
            "workerMcpSecurity": "not_certified",
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crew-source-zip", type=Path, required=True)
    parser.add_argument("--dashboard-zip", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = check(args.crew_source_zip, args.dashboard_zip)
    data = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(data, encoding="utf8")
    print(data, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
