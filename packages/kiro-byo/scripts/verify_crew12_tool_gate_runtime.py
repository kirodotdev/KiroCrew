#!/usr/bin/env python3
"""Probe actual Crew 0.12.0 tool-gate answers before/after isolated BYO install.

Never touches a real Crew installation. A registry result is NOT a Worker/MCP
execution authorization proof. The subprocess import avoids module-cache drift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from kiro_byo import integration_manager as manager  # noqa: E402
from scripts.probe_crew12_runtime_registration import probe  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(crew_package: Path, app_root: Path = ROOT) -> dict:
    if not (crew_package / 'static/dist/index.html').is_file():
        raise ValueError('complete, combined Crew 0.12.0 Dashboard package required')
    with tempfile.TemporaryDirectory(prefix='kiro-byo-r74-gate-parity-') as td:
        base = Path(td)
        package = base / 'src/kiro_crew'
        shutil.copytree(crew_package, package)
        gate = package / 'agent_sdk/tool_gate.py'
        before_hash = sha(gate)
        unmodified = probe(package.parent)
        home = base / 'byo-app'
        with patch.object(manager, 'running_crew', lambda: ('0.12.0', package, package.parent)):
            installed = manager.install_from_app(app_root, home, version='0.3.2')
            if not installed.get('ok'):
                raise AssertionError('isolated R74 install failed')
            after_hash = sha(gate)
            integrated = probe(package.parent)
            uninstalled = manager.uninstall_from_app(app_root, home)
        if not uninstalled.get('ok'):
            raise AssertionError('isolated R74 uninstall failed')
        if before_hash != after_hash or sha(gate) != before_hash:
            raise AssertionError('R74 unexpectedly rewrote the retired Crew tool gate')
        if unmodified['hostRoutingVerdict'] != 'indeterminate' or integrated['hostRoutingVerdict'] != 'indeterminate':
            raise AssertionError('unknown route was silently promoted to safe')
        if unmodified['hostNonRoutedRefusalEnforced'] or integrated['hostNonRoutedRefusalEnforced']:
            raise AssertionError('host enforced-routing claim changed unexpectedly')
        if integrated['route'] != 'acp_permission_gate' or not integrated['selectable']:
            raise AssertionError('BYO registered integration route unexpectedly unavailable')
        if not integrated['hostLaunchDescriptionPresent']:
            raise AssertionError('BYO launch descriptor is missing')
        return {
            'status':'pass', 'sourceGateUnchangedDuringIntegration': True,
            'unmodified':unmodified, 'integrated':integrated,
            'toolPermissionE2E':'not_verified', 'desktopGatewayE2E':'not_verified',
            'scope':'actual Crew Python imports in fresh child processes, isolated filesystem',
        }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--crew-package', required=True, type=Path)
    p.add_argument('--app-root', type=Path, default=ROOT)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    result = audit(args.crew_package, args.app_root)
    encoded = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
