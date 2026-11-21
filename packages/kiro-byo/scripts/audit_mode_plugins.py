#!/usr/bin/env python3
"""Read-only installed Mode/Type integrity audit, with honest hot-swap capability labels.

This checks the BYO tree (and optionally the user's JSON policy directory) without
writing configuration or importing any user-supplied Python code. It cannot
prove that a live KiroCrew session has adopted a new Type.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from kiro_byo.mode_registry import load_mode_registry  # noqa: E402
from kiro_byo.modes import builtin_modes  # noqa: E402
from kiro_byo.policy_packs import public_policy_packs  # noqa: E402


def audit() -> dict:
    registry = load_mode_registry()
    public = public_policy_packs()
    items = []
    warnings = []
    errors = list(registry.errors)
    for declaration in builtin_modes():
        installed = registry.get(declaration.id)
        policy_path = declaration.policy_path
        digest = sha256(policy_path.read_bytes()).hexdigest() if policy_path.is_file() else None
        expected_types = list(declaration.type_ids)
        observed_types = list(installed.type_ids) if installed else []
        valid = bool(installed is not None and observed_types == expected_types
                     and installed.pack.default_policy == declaration.default_type
                     and installed.enabled == declaration.enabled)
        overridden = bool(installed is not None and installed.pack.source != 'builtin')
        if overridden:
            warnings.append(f'{declaration.id}: legacy custom policy overrides built-in; not stock strategy behavior')
        if not valid:
            errors.append({'mode': declaration.id, 'error': 'registered Mode/Type contract mismatch'})
        items.append({
            'id': declaration.id,
            'type_ids': observed_types,
            'default_type': installed.pack.default_policy if installed else None,
            'enabled': installed.enabled if installed else None,
            'source': installed.pack.source if installed else None,
            'plugin_version': installed.version if installed else None,
            'builtin_policy_sha256': digest,
            'contract_valid': valid,
            'legacy_override': overridden,
            'runtime_code_hot_swap': False,
            'managed_builtin_upgrade': False,
        })
    for plugin in registry.plugins:
        if plugin.id in {item['id'] for item in items}:
            continue
        items.append({
            'id': plugin.id, 'type_ids': list(plugin.type_ids),
            'default_type': plugin.pack.default_policy,
            'enabled': plugin.enabled, 'source': plugin.pack.source,
            'plugin_version': plugin.version, 'contract_valid': True,
            'runtime_code_hot_swap': False,
            'policy_data_hot_reload': True,
        })
    return {
        'result': 'fail' if errors else ('warning' if warnings else 'pass'),
        'plugin_kind': public['mode_plugin_kind'],
        'hot_reload_scope': public['hot_reload_scope'],
        'executable_hot_swap': False,
        'note': 'Static registry/read-time verification, not live KiroCrew UI or executed-route E2E',
        'modes': items, 'warnings': warnings, 'errors': errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, help='Optional output JSON file, otherwise stdout')
    args = parser.parse_args()
    report = audit()
    output = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding='utf-8')
    else:
        print(output, end='')
    return 1 if report['result'] == 'fail' else 0


if __name__ == '__main__':
    raise SystemExit(main())
